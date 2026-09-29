"""Tests for the Terrapod-native vs TFE-CLI API namespace split.

Terrapod-native routes are canonical at /api/v1/ and also served at the
deprecated /api/terrapod/v1/ alias (#1529). /api/v2/ is the permanent TFE V2
CLI-contract surface (terraform / tofu / tfci / go-tfe) and is unaffected — the
#278 guard below still holds: native routes never reappear under /api/v2/.

The alias is deliberately absent from the OpenAPI schema. It is a real, routable
path — the route contract pins it — but documenting both would double /api/docs
and leave a reader unsure which to use.
"""

from __future__ import annotations

from terrapod.api.app import app


class TestOpenAPIVisibility:
    def test_canonical_paths_in_schema(self) -> None:
        schema = app.openapi()
        for path in (
            "/api/v1/labels",
            "/api/v1/auth/providers",
            "/api/v1/listeners/{listener_id}/heartbeat",
            "/api/v1/gpg-keys",
            "/api/v1/admin/audit-log",
        ):
            assert path in schema["paths"], f"canonical path {path} missing from OpenAPI"

    def test_the_deprecated_alias_is_routable_but_undocumented(self) -> None:
        """Served, but not shown in /api/docs (#1529).

        Both halves matter. Absent from the schema, so a reader is not offered two
        paths for one endpoint and left guessing which is current; present in
        `app.routes`, because a lagging runner still calls it and dropping it
        would be a removal.
        """
        schema = app.openapi()
        routes = {getattr(r, "path", "") for r in app.routes}
        for path in ("/api/terrapod/v1/labels", "/api/terrapod/v1/gpg-keys"):
            assert path not in schema["paths"], f"{path} should not be documented"
            assert path in routes, f"{path} must still be served"

    def test_cli_surface_stays_at_v2_in_schema(self) -> None:
        """CLI-consumed paths are documented at the canonical TFE prefix (#1528).

        `/api/v2` still serves every one of them; it is excluded from the schema
        so /api/docs shows one path per endpoint rather than two.
        """
        schema = app.openapi()
        for path in (
            "/api/tfe/v2/ping",
            "/api/tfe/v2/runs",
            "/api/tfe/v2/runs/{run_id}",
            "/api/tfe/v2/state-versions/{state_version_id}/download",
            "/api/tfe/v2/registry/modules/{namespace}/{name}/{provider}/versions",
            "/api/tfe/v2/varsets/{varset_id}",
        ):
            assert path in schema["paths"], f"CLI surface path {path} missing from OpenAPI"
        # CLI-surface paths do not appear on the native surface unless Terrapod's
        # own consumers need them for a workspace the TFE surface cannot serve.
        #
        # `/api/v1/runs` is such an exception (#1572). The TFE surface correctly
        # 404s a non-Terraform workspace, so with runs mounted only there the UI
        # could not list or create a run for a Pulumi workspace at all. The two
        # paths are NOT the same endpoint documented twice — they answer
        # differently by design, one scoped to Terraform and one not — which is
        # why both are in the schema rather than one being hidden.
        #
        # The two below stay forbidden and for different reasons: a varset is
        # reached through an `organizations/default/` collection, which the
        # native surface must never carry (architecture principle 9); the
        # registry paths are the CLI download protocol, which has no native
        # consumer at all.
        for path in (
            "/api/v1/varsets/{varset_id}",
            "/api/v1/registry/modules/{namespace}/{name}/{provider}/versions",
        ):
            assert path not in schema["paths"], f"{path} should not exist"

        # The exception is deliberate, so pin it: if runs stop being served
        # natively, the Pulumi UI breaks and this says so rather than the
        # workspace page going blank.
        assert "/api/v1/runs" in schema["paths"], (
            "runs must stay on the native surface — the TFE surface 404s a "
            "Pulumi workspace, so this is the only door its runs have"
        )


class TestRouteTopology:
    def test_no_native_route_anywhere_carries_an_org_segment(self) -> None:
        """The general form of the rule below, which lists paths and so can only
        catch the mistakes someone thought of.

        Worth having because the cheapest way to give the native surface a route
        is to mount a TFE router there, and a TFE router may carry
        `organizations/default/` quite legitimately — so the violation arrives
        as a side effect of a mount rather than as a path anyone wrote. That is
        exactly how it happened: mounting the whole variables router natively to
        reach one field put `/api/v1/organizations/default/varsets` on the
        native surface (#1898).
        """
        # Pulumi's service-backend routes are exempt: `organizations/{org}` is
        # a segment of *Pulumi's* wire protocol, which we implement, not a
        # Terrapod org. The same distinction the TFE surface's
        # `organizations/default/` rests on — a foreign protocol's shape is not
        # ours to flatten. Narrow on purpose, so anything else still trips.
        offenders = sorted(
            path
            for r in app.routes
            if (path := getattr(r, "path", "")).startswith(("/api/v1/", "/api/terrapod/v1/"))
            and "/organizations/" in path
            and "/pulumi/" not in path
        )
        assert not offenders, (
            "the Terrapod-native surface is single-organization by design "
            f"(architecture principle 9), but these carry an org segment: {offenders}"
        )

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
