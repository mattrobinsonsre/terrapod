"""Tests for the Terrapod-native vs TFE-CLI API namespace split.

Post-#278 there is no dual-mount: Terrapod-native routes live *only*
at /api/terrapod/v1/. /api/v2/ is the permanent TFE V2 CLI-contract
surface (terraform / tofu / tfci / go-tfe) and is unaffected.

These assert: canonical Terrapod paths are in the schema and routable;
the CLI surface stays at /api/v2/; the Terrapod surface never carries
an /organizations/default/ segment; and the old /api/v2/ aliases of
moved routes are well and truly gone (the #278 regression guard).
"""

from __future__ import annotations

from terrapod.api.app import app


class TestOpenAPIVisibility:
    def test_canonical_paths_in_schema(self) -> None:
        schema = app.openapi()
        for path in (
            "/api/terrapod/v1/labels",
            "/api/terrapod/v1/auth/providers",
            "/api/terrapod/v1/listeners/{listener_id}/heartbeat",
            "/api/terrapod/v1/gpg-keys",
            "/api/terrapod/v1/admin/audit-log",
        ):
            assert path in schema["paths"], f"canonical path {path} missing from OpenAPI"

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
        for path in (
            "/api/terrapod/v1/runs",
            "/api/terrapod/v1/varsets/{varset_id}",
            "/api/terrapod/v1/registry/modules/{namespace}/{name}/{provider}/versions",
        ):
            assert path not in schema["paths"], f"{path} should not exist"


class TestRouteTopology:
    def test_terrapod_native_paths_have_no_org_segment(self) -> None:
        """Per CLAUDE.md rule #9, the Terrapod-native surface must never
        carry an `organizations/default/` segment.
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

    def test_legacy_v2_aliases_removed(self) -> None:
        """#278 regression guard: the transitional /api/v2/ aliases of
        Terrapod-native routes are gone. Only the CLI surface remains at
        /api/v2/.
        """
        paths = {getattr(r, "path", "") for r in app.routes}
        for path in (
            # moved-router aliases
            "/api/v2/labels",
            "/api/v2/auth/providers",
            "/api/v2/auth/callback",
            "/api/v2/listeners/{listener_id}/heartbeat",
            "/api/v2/admin/audit-log",
            "/api/v2/gpg-keys",
            # org-scoped pre-v0.23 shapes
            "/api/v2/organizations/default/users",
            "/api/v2/organizations/default/vcs-connections",
            "/api/v2/organizations/default/agent-pools",
            "/api/v2/organizations/default/registry-modules",
            "/api/v2/organizations/default/registry-providers",
            # gpg keys' historical non-/api/v2 prefix
            "/api/registry/private/v2/gpg-keys",
        ):
            assert path not in paths, (
                f"legacy alias {path} is still routable — #278 removes all "
                f"Terrapod-native /api/v2 aliases"
            )

    def test_auth_callback_only_on_terrapod_prefix(self) -> None:
        """The OAuth/SAML callback is Terrapod-native; it must resolve at
        /api/terrapod/v1/auth/* and nowhere under /api/v2/.
        """
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/terrapod/v1/auth/callback" in paths
        assert "/api/terrapod/v1/auth/saml/acs" in paths
        assert "/api/v2/auth/callback" not in paths
        assert "/api/v2/auth/saml/acs" not in paths
