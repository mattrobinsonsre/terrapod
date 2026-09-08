"""The native surface is served at both prefixes, consistently (#1529).

`/api/v1` is canonical; `/api/terrapod/v1` is a deprecated alias kept for the
support window because our own runner and listener images construct it directly
and are expected to lag the API by minors.

Dual-mounting is the easy half. These tests cover the half that goes wrong: code
that *matches* on a path and decides something. Each of the three below fails
silently rather than loudly if it knows only one prefix — a weakened rate limit,
a misrouted follower request, an audit entry attributed to nothing — so none of
them would be caught by a route test or by the contract snapshot.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from terrapod.api.prefixes import (
    NATIVE_LEGACY_PREFIX,
    NATIVE_PREFIX,
    PEER_PREFIX,
    canonical_path,
)

_APP = pathlib.Path(__file__).resolve().parents[2] / "terrapod/api/app.py"


class TestBothPrefixesAreServed:
    def test_every_native_route_exists_at_both_prefixes(self) -> None:
        """The alias is not partial.

        A router mounted at the canonical prefix only would drop routes a lagging
        listener still calls — a removal, and the kind the route-contract gate
        reports as breaking.
        """
        from terrapod.api.app import app

        canonical = {
            r.path[len(NATIVE_PREFIX) :]
            for r in app.routes
            if getattr(r, "path", "").startswith(NATIVE_PREFIX + "/")
        }
        legacy = {
            r.path[len(NATIVE_LEGACY_PREFIX) :]
            for r in app.routes
            if getattr(r, "path", "").startswith(NATIVE_LEGACY_PREFIX + "/")
        }
        assert canonical, "no routes found at the canonical prefix"
        assert canonical == legacy, (
            "the two native prefixes serve different route sets; "
            f"canonical-only={sorted(canonical - legacy)[:5]} "
            f"alias-only={sorted(legacy - canonical)[:5]}"
        )

    def test_routers_mount_through_the_helper_only(self) -> None:
        """No router may be mounted at the native prefix directly.

        The helper is what guarantees both prefixes. A direct
        `app.include_router(x, prefix=TERRAPOD_PREFIX)` mounts the canonical path
        alone and silently omits the alias — which is exactly what seven routers
        were doing before #1529, and nothing flagged it.
        """
        tree = ast.parse(_APP.read_text())
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "include_router":
                continue
            for kw in node.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Name):
                    if kw.value.id == "TERRAPOD_PREFIX":
                        offenders.append(node.lineno)
        # The helper's own body is the single legitimate use.
        assert len(offenders) <= 1, (
            f"app.py mounts a router at TERRAPOD_PREFIX directly at line(s) {offenders} — "
            "use include_terrapod() so the deprecated alias is served too"
        )


class TestPathNormalisation:
    @pytest.mark.parametrize(
        ("given", "want"),
        [
            (f"{NATIVE_LEGACY_PREFIX}/workspaces", f"{NATIVE_PREFIX}/workspaces"),
            (f"{NATIVE_PREFIX}/workspaces", f"{NATIVE_PREFIX}/workspaces"),
            # Anything off the native surface is returned untouched, so this is
            # safe to apply to every request.
            ("/api/v2/workspaces", "/api/v2/workspaces"),
            ("/oauth/token", "/oauth/token"),
            ("/v2/library/nginx/manifests/latest", "/v2/library/nginx/manifests/latest"),
        ],
    )
    def test_canonicalises_only_the_native_surface(self, given: str, want: str) -> None:
        assert canonical_path(given) == want


class TestTheDecisionsMadeOnPaths:
    """Each of these is a control that would weaken quietly, not fail loudly."""

    @pytest.mark.parametrize("prefix", [NATIVE_PREFIX, NATIVE_LEGACY_PREFIX])
    def test_login_endpoints_get_the_strict_rate_limit_on_both(self, prefix: str) -> None:
        """Otherwise password guessing via the alias gets the general limit."""
        from terrapod.api.rate_limit import _is_auth_path

        assert _is_auth_path(f"{prefix}/auth/local/login")
        assert _is_auth_path(f"{prefix}/auth/local/authorize")

    def test_the_oauth_token_exception_still_holds(self) -> None:
        """A negative case, so the normalisation cannot have widened the bucket."""
        from terrapod.api.rate_limit import _is_auth_path

        assert not _is_auth_path("/oauth/token")

    def test_a_non_auth_native_path_is_not_in_the_strict_bucket(self) -> None:
        from terrapod.api.rate_limit import _is_auth_path

        assert not _is_auth_path(f"{NATIVE_PREFIX}/workspaces")

    @pytest.mark.parametrize("prefix", [NATIVE_PREFIX, NATIVE_LEGACY_PREFIX])
    def test_a_follower_serves_the_same_requests_on_both(self, prefix: str) -> None:
        """A listener enrolled via the alias must not be refused by a follower."""
        from terrapod.api.follower_gate import is_follower_writable

        assert is_follower_writable(f"{prefix}/auth/local/login")
        assert is_follower_writable(f"{prefix}/listeners/listener-abc/heartbeat")
        assert is_follower_writable(f"{prefix}/agent-pools/apool-1/listeners/join")

    @pytest.mark.parametrize("prefix", [NATIVE_PREFIX, NATIVE_LEGACY_PREFIX])
    def test_a_follower_still_refuses_platform_writes_on_both(self, prefix: str) -> None:
        """The negative path matters more: normalisation must not widen the gate."""
        from terrapod.api.follower_gate import is_follower_writable

        assert not is_follower_writable(f"{prefix}/agent-pools")
        assert not is_follower_writable(f"{prefix}/workspaces")
        assert not is_follower_writable(f"{prefix}/roles")

    @pytest.mark.parametrize("prefix", [NATIVE_PREFIX, NATIVE_LEGACY_PREFIX])
    def test_audit_attributes_the_resource_on_both(self, prefix: str) -> None:
        """Without this the entry falls through to the first path segment and is
        recorded against "api" — an audit trail that identifies nothing."""
        from terrapod.services.audit_service import parse_resource

        assert parse_resource(f"{prefix}/workspaces/ws-abc123") == ("workspaces", "ws-abc123")
        assert parse_resource(f"{prefix}/users/a@b.c") == ("users", "a@b.c")

    def test_the_tfe_surface_is_still_attributed(self) -> None:
        """A regression guard: adding /api/v1 must not have disturbed /api/v2."""
        from terrapod.services.audit_service import parse_resource

        assert parse_resource("/api/v2/workspaces/ws-1") == ("workspaces", "ws-1")
        assert parse_resource("/api/v2/ping") == ("ping", "")


class TestPeerCalls:
    def test_node_to_node_urls_stay_on_the_legacy_prefix(self) -> None:
        """HA replication must survive a rolling upgrade.

        A peer on an older release does not serve `/api/v1`. Pointing replication
        at the canonical prefix would 404 against it — and a failed replication
        call is a silent divergence between nodes, not a visible error. This flips
        only once every supported release serves the canonical prefix.
        """
        assert PEER_PREFIX == NATIVE_LEGACY_PREFIX

    def test_no_ha_module_hardcodes_a_prefix(self) -> None:
        """So the flip above is one edit, not a hunt."""
        root = pathlib.Path(__file__).resolve().parents[2] / "terrapod/services"
        offenders = []
        for name in ("ha_role.py", "blob_sync.py", "replication_sync.py"):
            src = (root / name).read_text()
            for i, line in enumerate(src.split("\n"), 1):
                st = line.strip()
                if st.startswith("#") or st.startswith("*"):
                    continue
                # No leading-quote requirement: every peer call is an
                # f-string, so `f"{base}/api/v1/ha/whoami"` has `}` before the
                # path and the stricter pattern missed exactly the regression
                # this guard exists to catch.
                if "/api/terrapod/v1" in line or "/api/v1" in line:
                    offenders.append(f"{name}:{i}")
        assert not offenders, (
            f"these hardcode an API prefix instead of using PEER_PREFIX: {offenders}"
        )


class TestTheSSOCallbackSwitch:
    """The one URL an alias cannot rescue (#1529).

    Everything else in this change is either inbound (a client calls a path we
    serve — the alias covers it) or asserted-and-followed (we hand a client a URL
    and it follows it — either prefix works). The SSO callback is neither: we
    assert a URL that a *third party validates against its own allow-list*. Send
    one the IdP does not recognise and the authorization request is refused
    there, before it reaches us, and every login fails.

    So the prefix for these URLs is an explicit operator switch, not a
    consequence of the routing change.
    """

    def test_defaults_to_the_legacy_prefix_so_upgrading_cannot_break_sso(self) -> None:
        from terrapod.config import Settings

        assert Settings().auth.legacy_callback_url is True

    def test_the_switch_selects_the_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from terrapod.api.routers import auth as auth_router

        monkeypatch.setattr(auth_router.settings.auth, "legacy_callback_url", True)
        assert auth_router._sso_url_prefix() == NATIVE_LEGACY_PREFIX

        monkeypatch.setattr(auth_router.settings.auth, "legacy_callback_url", False)
        assert auth_router._sso_url_prefix() == NATIVE_PREFIX

    def test_no_sso_url_hardcodes_a_prefix(self) -> None:
        """Otherwise the switch silently governs only some of them.

        There are four sites — two OIDC authorize calls, the OIDC token exchange,
        and the SAML ACS — and the token exchange must send byte-identical values
        to the authorize call or the IdP rejects it. One left behind is a broken
        login, not a cosmetic inconsistency.
        """
        path = pathlib.Path(__file__).resolve().parents[2] / "terrapod/api/routers/auth.py"
        src = path.read_text()

        # Exclude docstring bodies via the AST rather than by looking for comment
        # markers: this rule is *explained* in a docstring that necessarily names
        # both prefixes, and a guard that fires on its own rationale is a guard
        # someone deletes.
        doc_lines: set[int] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str) and node.end_lineno:
                    doc_lines.update(range(node.lineno, node.end_lineno + 1))

        offenders = [
            f"auth.py:{i}"
            for i, line in enumerate(src.split("\n"), 1)
            if i not in doc_lines
            and ("/auth/callback" in line or "/auth/saml/acs" in line)
            and ("/api/v1" in line or "/api/terrapod/v1" in line)
            and not line.strip().startswith(("#", "*"))
        ]
        assert not offenders, (
            f"these build an IdP-facing URL with a hardcoded prefix: {offenders} — "
            "use _sso_url_prefix() so the operator switch governs all of them"
        )


class TestTheDeprecationSignal:
    """docs/deprecations.md tells automated clients to watch for these (#1529).

    Until this change nothing in the codebase called `mark_deprecated`, so the
    first real deprecation would have shipped without the machine-readable
    signal its own published policy promises. A prose-only deprecation is worse
    than none: it tells operators to automate against a header that never comes.
    """

    async def test_the_alias_advertises_its_sunset(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from terrapod.api.app import create_application

        app = create_application()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"{NATIVE_LEGACY_PREFIX}/auth/providers")

        assert r.headers.get("deprecation") == "true", (
            f"the deprecated alias must advertise itself; headers were {dict(r.headers)}"
        )
        assert "sunset" in r.headers, "RFC 8594 Sunset header missing on the alias"
        assert "deprecation" in r.headers.get("link", "").lower()

    async def test_the_canonical_prefix_carries_no_such_header(self) -> None:
        """The negative half. A signal on everything is a signal on nothing —
        and a client would conclude the canonical path is going away too."""
        from httpx import ASGITransport, AsyncClient

        from terrapod.api.app import create_application

        app = create_application()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"{NATIVE_PREFIX}/auth/providers")

        assert "deprecation" not in r.headers
        assert "sunset" not in r.headers

    def test_the_published_sunset_matches_the_docs(self) -> None:
        """The header and docs/deprecations.md must name the same date.

        A `Sunset` that contradicts the published table is worse than silence:
        an operator automating on the header plans for one date while the page
        they were pointed at says another.
        """
        from terrapod.api.prefixes import NATIVE_ALIAS_SUNSET

        here = pathlib.Path(__file__).resolve()
        doc_path = next(
            (c for c in (b / "docs/deprecations.md" for b in here.parents[1:6]) if c.is_file()),
            None,
        )
        assert doc_path is not None, (
            "docs/deprecations.md not found from either layout — if the test image "
            "stopped copying it, this guard went quiet rather than red"
        )
        doc = doc_path.read_text()
        assert NATIVE_ALIAS_SUNSET.isoformat() in doc, (
            f"the alias advertises a sunset of {NATIVE_ALIAS_SUNSET.isoformat()} "
            "but docs/deprecations.md does not mention that date"
        )
