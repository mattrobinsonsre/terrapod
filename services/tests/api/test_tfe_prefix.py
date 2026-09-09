"""The TFE surface and the provider mirror move prefix (#1528).

`/api/tfe/v2` names the compatibility layer for what it is, and takes it out of
Terrapod's own version namespace so `/api/v2` is not permanently spent on another
product's protocol. The mirror joins the pull-through caches under `/api/v1`.
Both former paths keep serving for the window.

The tests worth having here are not "does the route exist" — the contract
snapshot covers that. They are the boundaries a moved URL crosses, because #1529
produced four blockers and every one was a URL leaving a boundary nobody had
listed: a client matching by path, an image with a compiled-in literal, a third
party validating against its own allow-list.
"""

from __future__ import annotations

import pathlib

import pytest

from terrapod.api.prefixes import (
    MIRROR_LEGACY_PREFIX,
    MIRROR_PREFIX,
    TFE_LEGACY_PREFIX,
    TFE_PREFIX,
    metric_path,
)


class TestBothPrefixesServeTheSameSurface:
    def test_every_tfe_route_exists_at_both_prefixes(self) -> None:
        """The alias is not a partial copy.

        A route present only on the canonical prefix is a removal for every
        `terraform`/`tofu` client and every runner image already in the field.
        """
        from terrapod.api.app import app

        canonical = {
            r.path[len(TFE_PREFIX) :]
            for r in app.routes
            if getattr(r, "path", "").startswith(TFE_PREFIX + "/")
        }
        legacy = {
            r.path[len(TFE_LEGACY_PREFIX) :]
            for r in app.routes
            if getattr(r, "path", "").startswith(TFE_LEGACY_PREFIX + "/")
            and not getattr(r, "path", "").startswith(TFE_PREFIX)
        }
        assert canonical, "no routes at the canonical TFE prefix"
        assert canonical == legacy, (
            f"prefixes disagree; canonical-only={sorted(canonical - legacy)[:5]} "
            f"alias-only={sorted(legacy - canonical)[:5]}"
        )

    def test_the_mirror_serves_at_both_prefixes(self) -> None:
        from terrapod.api.app import app

        paths = {getattr(r, "path", "") for r in app.routes}
        for suffix in ("/{hostname}/{namespace}/{type}/index.json",):
            assert f"{MIRROR_PREFIX}{suffix}" in paths
            assert f"{MIRROR_LEGACY_PREFIX}{suffix}" in paths


class TestServiceDiscovery:
    """Discovery is what moves clients, and the spike proved they honour it."""

    async def test_it_advertises_the_canonical_paths(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from terrapod.api.app import create_application

        app = create_application()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/.well-known/terraform.json")
        assert r.status_code == 200
        body = r.json()

        for key in ("tfe.v2", "tfe.v2.1", "tfe.v2.2"):
            assert body[key] == f"{TFE_PREFIX}/", f"{key} still advertises the old prefix"
        assert body["modules.v1"] == f"{TFE_PREFIX}/registry/modules/"
        assert body["providers.v1"] == f"{TFE_PREFIX}/registry/providers/"

    def test_every_advertised_path_is_actually_served(self) -> None:
        """A discovery document naming a path nothing serves is the worst case.

        The client believes it and derives every subsequent request from it, so
        the failure is a 404 on the first real call rather than anything visible
        at startup.
        """
        from terrapod.api.app import app

        paths = {getattr(r, "path", "") for r in app.routes}
        for advertised in (
            f"{TFE_PREFIX}/ping",
            f"{TFE_PREFIX}/registry/modules/{{namespace}}/{{name}}/{{provider}}/versions",
            f"{TFE_PREFIX}/registry/providers/{{namespace}}/{{name}}/versions",
        ):
            assert advertised in paths, f"discovery would advertise unserved {advertised}"


class TestUrlsThatBypassDiscovery:
    """The finding that made this issue worth doing carefully.

    `hosted-*-url` are absolute URLs the client follows verbatim — the spike
    watched a real `tofu apply` walk straight past the advertised base to these.
    Move discovery without them and `init`/`plan` look perfect while state upload
    keeps using the old path, on the one endpoint that deliberately requires no
    auth.
    """

    def test_hosted_state_urls_use_the_canonical_prefix(self) -> None:
        src = (
            pathlib.Path(__file__).resolve().parents[2] / "terrapod/api/routers/tfe_v2.py"
        ).read_text()
        for field in (
            "hosted-state-download-url",
            "hosted-state-upload-url",
            "hosted-json-state-upload-url",
        ):
            line = next(ln for ln in src.split("\n") if field in ln and 'f"' in ln)
            assert "TFE_PREFIX" in line, (
                f"{field} hardcodes a prefix instead of using TFE_PREFIX: {line.strip()}"
            )


class TestConsumersThatCannotBeTold:
    """Images and dashboards hold literals; they cannot learn a new path."""

    def test_the_runner_still_writes_paths_the_api_serves(self) -> None:
        """The runner overrides discovery for terraform inside the Job.

        Those values are compiled into whichever image is running, and a runner
        lags the API by design — so whatever it writes must remain served. This
        asserts the pairing rather than the literal, so the day the runner moves,
        the test moves with it and still means something.
        """
        from terrapod.api.app import app

        src = (
            pathlib.Path(__file__).resolve().parents[2] / "terrapod/runner/phases/mirror_config.py"
        ).read_text()
        served = {getattr(r, "path", "") for r in app.routes}

        # The mirror base the runner hands terraform.
        assert f"{{api_url}}{MIRROR_LEGACY_PREFIX}/" in src.replace('"', "").replace("'", "") or (
            MIRROR_LEGACY_PREFIX in src
        ), "the runner no longer writes a mirror base this test recognises"
        assert any(p.startswith(MIRROR_LEGACY_PREFIX + "/") for p in served), (
            f"the runner writes a mirror URL under {MIRROR_LEGACY_PREFIX} but nothing serves it"
        )

        # The discovery overrides it writes.
        for advertised in ("tfe.v2", "modules.v1", "providers.v1"):
            assert advertised in src
        assert any(p.startswith(TFE_LEGACY_PREFIX + "/") for p in served), (
            f"the runner overrides discovery to {TFE_LEGACY_PREFIX} but nothing serves it"
        )

    @pytest.mark.parametrize(
        ("given", "want"),
        [
            (f"{TFE_PREFIX}/workspaces/{{id}}", f"{TFE_LEGACY_PREFIX}/workspaces/{{id}}"),
            (f"{TFE_LEGACY_PREFIX}/workspaces/{{id}}", f"{TFE_LEGACY_PREFIX}/workspaces/{{id}}"),
            (f"{MIRROR_PREFIX}/h/n/t/index.json", f"{MIRROR_LEGACY_PREFIX}/h/n/t/index.json"),
            ("/oauth/token", "/oauth/token"),
            ("/v2/library/nginx/manifests/latest", "/v2/library/nginx/manifests/latest"),
        ],
    )
    def test_metrics_fold_to_one_stable_label(self, given: str, want: str) -> None:
        """One endpoint, one series — and the label does not rename mid-window.

        A dashboard is a consumer holding a literal. Splitting the series makes a
        panel under-report silently; renaming it makes the panel go blank. Both
        are avoided by folding onto the name that was already there.
        """
        assert metric_path(given) == want


class TestAuditAttribution:
    @pytest.mark.parametrize("prefix", [TFE_PREFIX, TFE_LEGACY_PREFIX])
    def test_the_resource_is_attributed_on_both(self, prefix: str) -> None:
        """Otherwise every entry falls through to the first path segment and is
        recorded against "api" — an audit trail that identifies nothing."""
        from terrapod.services.audit_service import parse_resource

        assert parse_resource(f"{prefix}/workspaces/ws-abc123") == ("workspaces", "ws-abc123")
