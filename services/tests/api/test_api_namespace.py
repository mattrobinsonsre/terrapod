"""Tests for the /api/v2 → /api/terrapod/v1 dual-mount and deprecation contract.

These tests assert the runtime behaviour of `include_moved` (in app.py):
canonical and legacy paths both serve traffic; only the legacy path
emits the deprecation headers; only the canonical path appears in
OpenAPI; the CLI surface stays put on /api/v2.

When the legacy aliases are removed in v0.24.0 (#278) most of these
will fail — the dual-serve assertions are exactly what a future
maintainer should drop.
"""

from __future__ import annotations

import httpx
import pytest

from terrapod.api.app import app


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestOpenAPIVisibility:
    """Deprecated aliases must not pollute the OpenAPI schema."""

    def test_canonical_paths_in_schema(self) -> None:
        schema = app.openapi()
        # Pick a few canonical Terrapod-native paths that should be in the
        # schema (one per major moved router).
        for path in (
            "/api/terrapod/v1/labels",
            "/api/terrapod/v1/auth/providers",
            "/api/terrapod/v1/listeners/{listener_id}/heartbeat",
            "/api/terrapod/v1/gpg-keys",
            "/api/terrapod/v1/admin/audit-log",
        ):
            assert path in schema["paths"], f"canonical path {path} missing from OpenAPI"

    def test_deprecated_aliases_not_in_schema(self) -> None:
        """The /api/v2 aliases of moved routes must NOT show in OpenAPI."""
        schema = app.openapi()
        for path in (
            "/api/v2/labels",
            "/api/v2/auth/providers",
            "/api/v2/listeners/{listener_id}/heartbeat",
        ):
            assert path not in schema["paths"], (
                f"deprecated alias {path} leaked into OpenAPI — include_in_schema=False missing?"
            )

    def test_cli_surface_stays_at_v2_in_schema(self) -> None:
        """CLI/tfci-consumed paths are at /api/v2/ in the schema, not under /api/terrapod/v1/."""
        schema = app.openapi()
        for path in (
            "/api/v2/ping",
            "/api/v2/runs",
            "/api/v2/runs/{run_id}",
            "/api/v2/state-versions/{state_version_id}/download",
            "/api/v2/registry/modules/{namespace}/{name}/{provider}/versions",
            "/api/v2/varsets/{varset_id}",
        ):
            assert path in schema["paths"], f"CLI surface path {path} missing from OpenAPI"
        # And these CLI paths must not also show under the Terrapod prefix.
        for path in (
            "/api/terrapod/v1/runs",
            "/api/terrapod/v1/varsets/{varset_id}",
            "/api/terrapod/v1/registry/modules/{namespace}/{name}/{provider}/versions",
        ):
            assert path not in schema["paths"], f"{path} should not exist"


class TestRouteTopology:
    """The actual app.routes table must include both canonical and legacy mounts.

    OpenAPI schema visibility is decoupled (asserted above); these checks
    are about whether requests can route at all.
    """

    def test_canonical_and_legacy_both_routable(self) -> None:
        """Every dual-mounted path resolves on both prefixes."""
        paths = {getattr(r, "path", "") for r in app.routes}
        for canonical, legacy in (
            ("/api/terrapod/v1/labels", "/api/v2/labels"),
            ("/api/terrapod/v1/auth/providers", "/api/v2/auth/providers"),
            (
                "/api/terrapod/v1/listeners/{listener_id}/heartbeat",
                "/api/v2/listeners/{listener_id}/heartbeat",
            ),
        ):
            assert canonical in paths, f"canonical {canonical} not routable"
            assert legacy in paths, f"legacy alias {legacy} not routable"

    def test_gpg_keys_legacy_alias_uses_historical_prefix(self) -> None:
        """gpg_keys lived at /api/registry/private/v2/, not /api/v2/."""
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/terrapod/v1/gpg-keys" in paths
        assert "/api/registry/private/v2/gpg-keys" in paths
        # NOT at /api/v2 — it never lived there.
        assert "/api/v2/gpg-keys" not in paths

    def test_terrapod_native_paths_have_no_org_segment(self) -> None:
        """Per CLAUDE.md rule #9, the canonical /api/terrapod/v1 surface
        must never carry an `organizations/default/` segment — Terrapod
        is single-org and the segment is dead weight outside the
        TFE-compat layer.
        """
        paths = {getattr(r, "path", "") for r in app.routes}
        for path in (
            "/api/terrapod/v1/organizations/default/users",
            "/api/terrapod/v1/organizations/default/vcs-connections",
            "/api/terrapod/v1/organizations/default/agent-pools",
            "/api/terrapod/v1/organizations/default/registry-modules",
            "/api/terrapod/v1/organizations/default/registry-providers",
        ):
            assert path not in paths, (
                f"canonical path {path} leaks /organizations/default/ — Terrapod-native "
                f"paths must use the short form (e.g. /api/terrapod/v1/users)"
            )

    def test_legacy_aliases_preserve_pre_v0_23_shapes(self) -> None:
        """For routers that pre-v0.23 lived under
        /api/v2/organizations/default/{resource}, the legacy alias at
        that exact path must keep working through v0.23.x for v0.22
        clients. We do NOT introduce /api/v2/{resource} (without the
        org segment) — that path never existed pre-v0.23 and has no
        callers.
        """
        paths = {getattr(r, "path", "") for r in app.routes}
        # Pre-v0.23 paths still routable.
        for path in (
            "/api/v2/organizations/default/users",
            "/api/v2/organizations/default/vcs-connections",
            "/api/v2/organizations/default/agent-pools",
            "/api/v2/organizations/default/registry-modules",
            "/api/v2/organizations/default/registry-providers",
        ):
            assert path in paths, f"legacy alias {path} missing — v0.22 callers will break"
        # /api/v2/{resource} (without org) was never published — must not
        # exist as a deprecated alias.
        for path in (
            "/api/v2/users",
            "/api/v2/vcs-connections",
            "/api/v2/registry-modules",
            "/api/v2/registry-providers",
        ):
            assert path not in paths, (
                f"{path} should not exist — pre-v0.23 callers used the "
                f"/organizations/default/ form, and {path} was never published"
            )

    def test_workspace_delete_only_at_canonical(self) -> None:
        """Same path under both prefixes for different methods is fine —
        but we assert specifically that the DELETE on /workspaces/{id}
        isn't accidentally back on the TFE-spec router."""
        # The route exists under both /api/v2 (deprecated alias) and
        # /api/terrapod/v1 (canonical) for the DELETE method.
        delete_routes = [
            r
            for r in app.routes
            if getattr(r, "path", "")
            in (
                "/api/v2/workspaces/{workspace_id}",
                "/api/terrapod/v1/workspaces/{workspace_id}",
            )
            and "DELETE" in getattr(r, "methods", set())
        ]
        # Two routes total: canonical + deprecated alias.
        assert len(delete_routes) == 2, (
            f"expected DELETE on both prefixes, got {len(delete_routes)}: {delete_routes}"
        )


class TestDeprecationHeaders:
    """The middleware must emit Deprecation/Link/X-Removed-In on legacy
    paths only — not on canonical paths or on CLI-surface /api/v2 routes.
    """

    @pytest.mark.asyncio
    async def test_legacy_alias_emits_deprecation_headers(self, client: httpx.AsyncClient) -> None:
        async with client:
            # /auth/providers doesn't require DB or Redis — pure connector
            # registry lookup — so it works in unit-test context. We're
            # only asserting on response headers, not body.
            resp = await client.get("/api/v2/auth/providers")
        assert resp.headers.get("Deprecation") == "true", (
            f"missing Deprecation header on legacy alias; headers: {dict(resp.headers)}"
        )
        assert 'rel="deprecation"' in resp.headers.get("Link", "")
        assert resp.headers.get("X-Removed-In") == "v0.24.0"

    @pytest.mark.asyncio
    async def test_canonical_path_does_not_emit_deprecation_headers(
        self, client: httpx.AsyncClient
    ) -> None:
        async with client:
            resp = await client.get("/api/terrapod/v1/auth/providers")
        assert "Deprecation" not in resp.headers, (
            f"canonical path leaking deprecation header: {dict(resp.headers)}"
        )
        assert "X-Removed-In" not in resp.headers

    @pytest.mark.asyncio
    async def test_cli_surface_v2_path_does_not_emit_deprecation_headers(
        self, client: httpx.AsyncClient
    ) -> None:
        """/api/v2/ping is permanent CLI surface — not deprecated even
        though it's under /api/v2."""
        async with client:
            resp = await client.get("/api/v2/ping")
        assert "Deprecation" not in resp.headers
        assert "X-Removed-In" not in resp.headers
