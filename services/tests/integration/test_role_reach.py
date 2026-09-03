"""Role reach preview (#1456) — which workspaces a role grants on, and why.

Real DB throughout, because the point of the feature is the SQL: allow and deny
are both evaluated as JSONB containment / name membership so counts stay
aggregate at fleet scale. A mocked session would assert the shape of a query
nobody ran and prove nothing about whether it selects the right rows.
"""

import uuid

import pytest

from terrapod.db.models import Role
from terrapod.db.session import get_db_session
from terrapod.services import role_reach_service
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


@pytest.fixture(autouse=True)
def _as_admin(app):
    """Every test here runs as a platform admin: the preview is gated on
    admin/audit, and the feature under test is the reach calculation, not the
    gate (which has its own test below)."""
    set_auth(app, admin_user())


async def _ws(client, name, labels=None, **attrs):
    resp = await client.post(
        WS_ENDPOINT,
        json={
            "data": {
                "type": "workspaces",
                "attributes": {"name": name, "labels": labels or {}, **attrs},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _role(client, name, **attrs):
    resp = await client.post(
        "/api/terrapod/v1/roles",
        json={"data": {"attributes": {"name": name, **attrs}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return name


class TestReachMatchesEnforcement:
    """The preview must agree with the gate that actually authorises, because a
    permissions view that disagrees with enforcement is worse than none."""

    async def test_allow_label_reaches_exactly_the_labelled_workspaces(self, app, client):
        tag = uuid.uuid4().hex[:8]
        hit = await _ws(client, f"reach-hit-{tag}", labels={"env": f"prod{tag}"})
        await _ws(client, f"reach-miss-{tag}", labels={"env": f"dev{tag}"})
        await _role(
            client,
            f"reach-role-{tag}",
            **{"allow-labels": {"env": [f"prod{tag}"]}, "workspace-permission": "write"},
        )

        resp = await client.get(f"/api/terrapod/v1/roles/reach-role-{tag}/preview", headers=AUTH)
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]

        assert attrs["granted-count"] == 1
        assert attrs["denied-count"] == 0
        names = [w["name"] for w in attrs["workspaces"]]
        assert names == [f"reach-hit-{tag}"]
        assert attrs["workspaces"][0]["id"] == hit
        assert attrs["workspaces"][0]["reason"] == f"allow-label:env=prod{tag}"
        # The capability set is the role's own, sliced to the workspace axis —
        # not a level name, since levels are authoring shorthand only.
        # `run:apply` is write-only, so its presence proves the role's grant is
        # reported rather than some read floor.
        assert "run:apply" in attrs["workspaces"][0]["capabilities"]

    async def test_the_verdict_agrees_with_the_enforcement_gate(self, app, client):
        """Same rule, both paths, every workspace — the equivalence that stops
        the preview drifting from `_role_matches`."""
        from terrapod.services.capability_resolver import MATCH_ALLOWED, _role_matches

        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"agree-a-{tag}", labels={"team": f"net{tag}"})
        await _ws(client, f"agree-b-{tag}", labels={"team": f"net{tag}", "tier": "x"})
        await _ws(client, f"agree-c-{tag}", labels={"team": f"other{tag}"})
        await _role(
            client,
            f"agree-role-{tag}",
            **{
                "allow-labels": {"team": [f"net{tag}"]},
                "deny-labels": {"tier": ["x"]},
                "workspace-permission": "read",
            },
        )

        async with get_db_session() as db:
            role = await db.get(Role, f"agree-role-{tag}")
            result = await role_reach_service.preview_role_reach(
                db, role, limit=100, viewer_roles=["admin"]
            )
            from sqlalchemy import select

            from terrapod.db.models import Workspace

            everything = (await db.execute(select(Workspace))).scalars().all()
            for ws in everything:
                granted = any(w["id"] == f"ws-{ws.id}" for w in result["workspaces"])
                assert granted == _role_matches(role, ws.name, ws.labels or {}), (
                    f"preview and enforcement disagree on {ws.name}"
                )
                if granted:
                    entry = next(w for w in result["workspaces"] if w["id"] == f"ws-{ws.id}")
                    assert entry["verdict"] == MATCH_ALLOWED


class TestDenyIsShownNotSilentlyOmitted:
    """ "matched 47, denied 3" is what makes a deny rule safe to write. An
    operator who cannot see what a deny removed cannot tell an intended
    exclusion from a typo."""

    async def test_denied_workspaces_are_reported_separately(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"deny-keep-{tag}", labels={"env": f"prod{tag}"})
        await _ws(client, f"deny-drop-{tag}", labels={"env": f"prod{tag}", "locked-down": "yes"})
        await _role(
            client,
            f"deny-role-{tag}",
            **{
                "allow-labels": {"env": [f"prod{tag}"]},
                "deny-labels": {"locked-down": ["yes"]},
                "workspace-permission": "write",
            },
        )

        resp = await client.get(f"/api/terrapod/v1/roles/deny-role-{tag}/preview", headers=AUTH)
        attrs = resp.json()["data"]["attributes"]

        assert attrs["granted-count"] == 1
        assert attrs["denied-count"] == 1
        assert attrs["matched-count"] == 2
        assert [w["name"] for w in attrs["workspaces"]] == [f"deny-keep-{tag}"]
        assert [w["name"] for w in attrs["denied"]] == [f"deny-drop-{tag}"]
        assert attrs["denied"][0]["reason"] == "deny-label:locked-down=yes"
        # Denied means denied: no capabilities are reported for it.
        assert attrs["denied"][0]["capabilities"] == []

    async def test_deny_name_beats_an_allow_label(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"dn-{tag}", labels={"env": f"prod{tag}"})
        await _role(
            client,
            f"dn-role-{tag}",
            **{
                "allow-labels": {"env": [f"prod{tag}"]},
                "deny-names": [f"dn-{tag}"],
                "workspace-permission": "admin",
            },
        )
        resp = await client.get(f"/api/terrapod/v1/roles/dn-role-{tag}/preview", headers=AUTH)
        attrs = resp.json()["data"]["attributes"]
        assert attrs["granted-count"] == 0
        assert attrs["denied-count"] == 1
        assert attrs["denied"][0]["reason"] == "deny-name"


class TestEmptyRulesReachNothing:
    """An empty allow set matches NOTHING, which is not the same as matching
    everything. Conflating the two would silently report a role as granting the
    entire estate."""

    async def test_a_role_with_no_allow_rules_reaches_nothing(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"empty-{tag}", labels={"env": "prod"})
        await _role(client, f"empty-role-{tag}", **{"workspace-permission": "admin"})
        resp = await client.get(f"/api/terrapod/v1/roles/empty-role-{tag}/preview", headers=AUTH)
        attrs = resp.json()["data"]["attributes"]
        assert attrs["granted-count"] == 0
        assert attrs["workspaces"] == []


class TestUnsavedPreview:
    """The authoring case: see the match while typing, before the rule exists."""

    async def test_an_unsaved_body_previews_without_persisting(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"uns-{tag}", labels={"squad": f"blue{tag}"})

        resp = await client.post(
            "/api/terrapod/v1/roles/preview",
            json={
                "data": {
                    "attributes": {
                        "name": f"never-saved-{tag}",
                        "allow-labels": {"squad": [f"blue{tag}"]},
                        "workspace-permission": "plan",
                    }
                }
            },
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["granted-count"] == 1

        # Nothing was persisted — a preview that created the role would be a
        # write dressed as a read.
        async with get_db_session() as db:
            assert await db.get(Role, f"never-saved-{tag}") is None

    async def test_an_invalid_rule_is_rejected_the_same_as_on_save(self, app, client):
        """A preview that accepts what a save rejects answers about a role that
        cannot exist."""
        resp = await client.post(
            "/api/terrapod/v1/roles/preview",
            json={"data": {"attributes": {"name": "bad", "workspace-permission": "sudo"}}},
            headers=AUTH,
        )
        assert resp.status_code == 422


class TestBuiltinsAndMissing:
    async def test_a_builtin_role_is_refused_rather_than_answered(self, app, client):
        """`admin` grants through the platform path on every workspace, so a
        label-reach answer would be true and deeply misleading."""
        resp = await client.get("/api/terrapod/v1/roles/admin/preview", headers=AUTH)
        assert resp.status_code == 422
        assert "built-in" in resp.json()["detail"]

    async def test_unknown_role_404s(self, app, client):
        resp = await client.get("/api/terrapod/v1/roles/nope-does-not-exist/preview", headers=AUTH)
        assert resp.status_code == 404


class TestPaging:
    """Counts are aggregates over the fleet, not over the page — an operator
    needs "this reaches 4,200" to be true, not truncated to what fitted."""

    async def test_counts_span_the_fleet_while_the_page_is_bounded(self, app, client):
        tag = uuid.uuid4().hex[:8]
        for i in range(5):
            await _ws(client, f"page-{tag}-{i}", labels={"grp": tag})
        await _role(
            client,
            f"page-role-{tag}",
            **{"allow-labels": {"grp": [tag]}, "workspace-permission": "read"},
        )

        resp = await client.get(
            f"/api/terrapod/v1/roles/page-role-{tag}/preview?page[size]=2", headers=AUTH
        )
        attrs = resp.json()["data"]["attributes"]
        assert attrs["granted-count"] == 5
        assert len(attrs["workspaces"]) == 2

        resp2 = await client.get(
            f"/api/terrapod/v1/roles/page-role-{tag}/preview?page[size]=2&page[number]=3",
            headers=AUTH,
        )
        assert len(resp2.json()["data"]["attributes"]["workspaces"]) == 1


class TestAxisCoverage:
    """A role's rules are matched the same way whatever they are matched
    against, so the preview must cover every axis they govern. Reporting only
    workspaces would answer a quarter of the question while looking complete.
    """

    async def test_one_rule_reaches_pools_and_registry_too(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"ax-ws-{tag}", labels={"squad": tag})
        pool = await client.post(
            "/api/terrapod/v1/agent-pools",
            json={
                "data": {
                    "type": "agent-pools",
                    "attributes": {"name": f"ax-pool-{tag}", "labels": {"squad": tag}},
                }
            },
            headers=AUTH,
        )
        assert pool.status_code == 201, pool.text

        await _role(
            client,
            f"ax-role-{tag}",
            **{
                "allow-labels": {"squad": tag},
                "workspace-permission": "write",
                "pool-permission": "admin",
            },
        )
        resp = await client.get(f"/api/terrapod/v1/roles/ax-role-{tag}/preview", headers=AUTH)
        axes = resp.json()["data"]["attributes"]["axes"]

        assert axes["workspace"]["granted-count"] == 1
        assert axes["pool"]["granted-count"] == 1, "the pool axis was not covered"

        # Capabilities are sliced PER AXIS: the same role reports workspace caps
        # against the workspace and pool caps against the pool, not one
        # undifferentiated set that is wrong on both.
        ws_caps = axes["workspace"]["resources"][0]["capabilities"]
        pool_caps = axes["pool"]["resources"][0]["capabilities"]
        assert all(c.split(":")[0] != "pool" for c in ws_caps), ws_caps
        assert all(c.startswith(("pool:", "agent-pool:")) for c in pool_caps), pool_caps


class TestResourceAccessView:
    """The reverse question: looking at one resource, who can reach it."""

    async def test_lists_the_roles_that_reach_a_workspace_and_who_holds_them(self, app, client):
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"acc-{tag}", labels={"env": f"prod{tag}"})
        await _role(
            client,
            f"acc-role-{tag}",
            **{"allow-labels": {"env": [f"prod{tag}"]}, "workspace-permission": "write"},
        )
        # PUT, not POST — and the status is asserted: the first version of this
        # test POSTed, got a silent 405, and still passed because nothing
        # checked that the assignment had landed.
        assigned = await client.put(
            "/api/terrapod/v1/role-assignments",
            json={
                "data": {
                    "attributes": {
                        "provider-name": "local",
                        "email": f"alice-{tag}@example.com",
                        "roles": [f"acc-role-{tag}"],
                    }
                }
            },
            headers=AUTH,
        )
        assert assigned.status_code in (200, 201), assigned.text

        resp = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/access", headers=AUTH)
        assert resp.status_code == 200, resp.text
        a = resp.json()["data"]["attributes"]

        names = [r["role"] for r in a["roles"]]
        assert f"acc-role-{tag}" in names
        entry = next(r for r in a["roles"] if r["role"] == f"acc-role-{tag}")
        assert entry["reason"] == f"allow-label:env=prod{tag}"
        assert "run:apply" in entry["capabilities"]
        # Who holds the role is the actionable half; a role nobody holds
        # reaches nothing in practice.
        assert entry["held-by"] == [f"alice-{tag}@example.com"], entry

        # Platform paths must be reported: a list of roles alone reads as the
        # complete answer when a platform admin reaches everything anyway.
        assert "platform-admin" in a["platform-paths"]
        assert "owner" in a["platform-paths"]

    async def test_a_denied_role_is_listed_separately_not_as_granting(self, app, client):
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"accd-{tag}", labels={"env": f"prod{tag}", "sealed": "yes"})
        await _role(
            client,
            f"accd-role-{tag}",
            **{
                "allow-labels": {"env": [f"prod{tag}"]},
                "deny-labels": {"sealed": ["yes"]},
                "workspace-permission": "admin",
            },
        )
        resp = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/access", headers=AUTH)
        a = resp.json()["data"]["attributes"]
        assert [r["role"] for r in a["roles"]] == []
        assert [r["role"] for r in a["denied-roles"]] == [f"accd-role-{tag}"]

    async def test_unknown_workspace_404s_and_a_bad_id_422s(self, app, client):
        assert (
            await client.get(f"/api/terrapod/v1/workspaces/ws-{uuid.uuid4()}/access", headers=AUTH)
        ).status_code == 404
        assert (
            await client.get("/api/terrapod/v1/workspaces/not-a-uuid/access", headers=AUTH)
        ).status_code == 422


class TestViewerMustAlreadySeeTheEstate:
    """The reach answer names every matching resource across the estate,
    regardless of what the caller can see. That is safe only because the
    endpoints are gated on platform admin/audit, who already see everything --
    so the coupling is explicit and fails CLOSED rather than resting on the
    gate staying as it is.
    """

    async def test_a_narrower_principal_is_refused_at_the_service(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"vis-{tag}", labels={"env": tag})
        await _role(
            client,
            f"vis-role-{tag}",
            **{"allow-labels": {"env": [tag]}, "workspace-permission": "read"},
        )
        async with get_db_session() as db:
            role = await db.get(Role, f"vis-role-{tag}")
            for roles in ([], ["everyone"], ["some-team-lead"]):
                with pytest.raises(role_reach_service.ViewerNotPermitted):
                    await role_reach_service.preview_role_reach(db, role, viewer_roles=roles)

    async def test_platform_audit_is_permitted(self, app, client):
        """Audit resolves to the read floor on every axis, so it already sees
        what the answer would disclose."""
        tag = uuid.uuid4().hex[:8]
        await _role(client, f"aud-role-{tag}", **{"workspace-permission": "read"})
        async with get_db_session() as db:
            role = await db.get(Role, f"aud-role-{tag}")
            out = await role_reach_service.preview_role_reach(db, role, viewer_roles=["audit"])
            assert out["granted-count"] == 0

    async def test_the_access_view_is_guarded_too(self, app, client):
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"visa-{tag}", labels={"env": tag})
        async with get_db_session() as db:
            from terrapod.db.models import Workspace

            ws = await db.get(Workspace, uuid.UUID(ws_id.removeprefix("ws-")))
            with pytest.raises(role_reach_service.ViewerNotPermitted):
                await role_reach_service.resolve_resource_access(
                    db, ws, axis="workspace", kind="workspaces", viewer_roles=["everyone"]
                )


class TestAllowAll:
    """The estate-wide grant. Label and name rules are exact-match, so before
    this the only way to cover the estate was a shared label on every
    workspace — which fails in the dangerous direction, since a workspace
    created without the label silently falls outside the role.
    """

    async def test_it_reaches_every_workspace_including_unlabelled_ones(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"aa-labelled-{tag}", labels={"env": tag})
        await _ws(client, f"aa-bare-{tag}")  # no labels at all
        await _role(
            client, f"aa-role-{tag}", **{"allow-all": True, "workspace-permission": "write"}
        )

        resp = await client.get(f"/api/terrapod/v1/roles/aa-role-{tag}/preview", headers=AUTH)
        a = resp.json()["data"]["attributes"]
        names = [w["name"] for w in a["axes"]["workspace"]["resources"]]
        assert f"aa-labelled-{tag}" in names
        assert f"aa-bare-{tag}" in names, "an unlabelled workspace must still be reached"
        assert a["axes"]["workspace"]["resources"][0]["reason"] == "allow-all"

    async def test_deny_still_wins_so_all_except_is_expressible(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"aad-keep-{tag}", labels={"grp": tag})
        await _ws(client, f"aad-drop-{tag}", labels={"grp": tag, "sealed": "yes"})
        await _role(
            client,
            f"aad-role-{tag}",
            **{
                "allow-all": True,
                "deny-labels": {"sealed": ["yes"]},
                "workspace-permission": "admin",
            },
        )
        resp = await client.get(f"/api/terrapod/v1/roles/aad-role-{tag}/preview", headers=AUTH)
        a = resp.json()["data"]["attributes"]
        granted = [w["name"] for w in a["axes"]["workspace"]["resources"]]
        denied = [w["name"] for w in a["axes"]["workspace"]["denied"]]
        assert f"aad-keep-{tag}" in granted
        assert f"aad-drop-{tag}" not in granted
        assert f"aad-drop-{tag}" in denied

    async def test_it_does_not_raise_the_roles_capabilities(self, app, client):
        """allow_all widens WHERE a role applies, never WHAT it grants."""
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"aac-{tag}")
        await _role(
            client, f"aac-role-{tag}", **{"allow-all": True, "workspace-permission": "read"}
        )
        resp = await client.get(f"/api/terrapod/v1/roles/aac-role-{tag}/preview", headers=AUTH)
        caps = resp.json()["data"]["attributes"]["axes"]["workspace"]["resources"][0][
            "capabilities"
        ]
        assert "run:apply" not in caps, caps
        assert any(c.endswith(":read") or c == "workspace:read" for c in caps), caps

    async def test_enforcement_agrees_with_the_preview(self, app, client):
        """The estate-wide grant must be honoured by the gate that actually
        authorises, not only by the panel that advertises it."""
        from terrapod.services.capability_resolver import _role_matches

        tag = uuid.uuid4().hex[:8]
        await _role(
            client, f"aae-role-{tag}", **{"allow-all": True, "workspace-permission": "read"}
        )
        async with get_db_session() as db:
            role = await db.get(Role, f"aae-role-{tag}")
            assert _role_matches(role, "any-name-at-all", {})
            assert _role_matches(role, "", {"unrelated": "label"})

    async def test_it_round_trips_through_the_api(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _role(
            client, f"aar-role-{tag}", **{"allow-all": True, "workspace-permission": "read"}
        )
        got = await client.get(f"/api/terrapod/v1/roles/aar-role-{tag}", headers=AUTH)
        assert got.json()["data"]["attributes"]["allow-all"] is True

        patched = await client.patch(
            f"/api/terrapod/v1/roles/aar-role-{tag}",
            json={"data": {"attributes": {"allow-all": False}}},
            headers=AUTH,
        )
        assert patched.json()["data"]["attributes"]["allow-all"] is False

    async def test_existing_roles_default_to_false(self, app, client):
        """Additive: every role that predates the column keeps exactly the
        reach it had."""
        tag = uuid.uuid4().hex[:8]
        await _role(client, f"aan-role-{tag}", **{"workspace-permission": "read"})
        got = await client.get(f"/api/terrapod/v1/roles/aan-role-{tag}", headers=AUTH)
        assert got.json()["data"]["attributes"]["allow-all"] is False


class TestAllowAllIsHonouredByEnforcement:
    """The preview advertises estate-wide reach; the gate must actually grant
    it, and must still refuse a denied resource. Both directions matter: the
    first is the feature, the second is the safety property."""

    async def test_the_gate_grants_on_an_unrelated_resource(self, app, client):
        from terrapod.services.capability_resolver import MATCH_ALLOWED, role_match_verdict

        tag = uuid.uuid4().hex[:8]
        await _role(client, f"aag-{tag}", **{"allow-all": True, "workspace-permission": "read"})
        async with get_db_session() as db:
            role = await db.get(Role, f"aag-{tag}")
            verdict, reason = role_match_verdict(role, "some-unrelated-name", {"any": "labels"})
            assert (verdict, reason) == (MATCH_ALLOWED, "allow-all")

    async def test_deny_beats_allow_all_AT_THE_GATE_not_just_in_the_sql(self, app, client):
        """The ordering invariant is a comment in `role_match_verdict`; the
        SQL-level test cannot see it, because `granted_query` computes deny
        independently. Move the allow_all check above the deny block and only
        this test notices."""
        from terrapod.services.capability_resolver import MATCH_DENIED, role_match_verdict

        tag = uuid.uuid4().hex[:8]
        await _role(
            client,
            f"aad2-{tag}",
            **{
                "allow-all": True,
                "deny-labels": {"sealed": ["yes"]},
                "workspace-permission": "admin",
            },
        )
        async with get_db_session() as db:
            role = await db.get(Role, f"aad2-{tag}")
            verdict, reason = role_match_verdict(role, "anything", {"sealed": "yes"})
            assert verdict == MATCH_DENIED, "deny must win over an estate-wide grant"
            assert reason == "deny-label:sealed=yes"

    async def test_a_holder_actually_resolves_capabilities_everywhere(self, app, client):
        """End of the chain: a principal holding an allow_all role resolves real
        capabilities on a workspace whose labels match nothing."""
        from terrapod.api.dependencies import AuthenticatedUser
        from terrapod.db.models import Workspace
        from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"aacap-{tag}")  # no labels at all
        await _role(client, f"aacap-{tag}", **{"allow-all": True, "workspace-permission": "write"})
        user = AuthenticatedUser(
            email=f"holder-{tag}@example.com",
            display_name="Holder",
            roles=[f"aacap-{tag}"],
            provider_name="local",
            auth_method="session",
        )
        async with get_db_session() as db:
            ws = await db.get(Workspace, uuid.UUID(ws_id.removeprefix("ws-")))
            caps = await resolve_workspace_capabilities_for(db, user, ws)
        assert "run:apply" in caps, caps


class TestRegistryAxisPaging:
    """The registry axis spans two models. Paging them independently returned
    up to twice the page size while skipping rows."""

    async def test_a_page_never_exceeds_its_size_across_two_models(self, app, client):
        tag = uuid.uuid4().hex[:8]
        for i in range(3):
            for kind in ("modules", "providers"):
                made = await client.post(
                    f"/api/terrapod/v1/registry-{kind}",
                    json={
                        "data": {
                            "attributes": {
                                "name": f"rp-{kind[:3]}-{tag}-{i}",
                                "provider": "aws",
                                "labels": {"grp": tag},
                            }
                        }
                    },
                    headers=AUTH,
                )
                # Asserted: without it a failed create leaves nothing to page
                # and the size assertion below passes vacuously.
                assert made.status_code == 201, made.text
        await _role(
            client,
            f"regpage-{tag}",
            **{"allow-labels": {"grp": tag}, "registry-permission": "read"},
        )
        resp = await client.get(
            f"/api/terrapod/v1/roles/regpage-{tag}/preview?page[size]=2", headers=AUTH
        )
        reg = resp.json()["data"]["attributes"]["axes"]["registry"]
        # The count must span both models, or the page assertion is meaningless.
        assert reg["granted-count"] == 6, reg
        assert len(reg["resources"]) <= 2, (
            f"page[size]=2 returned {len(reg['resources'])} — the two registry models "
            "were paged independently"
        )


class TestAgentPoolNamesAreServed:
    """A workspace's pools must be renderable without fetching the pool list.

    The web read-only view resolved names only from a list it fetched when
    entering EDIT mode, so before that every pool rendered as a bare
    `apool-<uuid>` — a UUID as the default thing an operator sees on the
    configuration tab. The server already had the names.
    """

    async def test_the_workspace_serializer_emits_names_matching_the_ids(self, app, client):
        tag = uuid.uuid4().hex[:8]
        pool = await client.post(
            "/api/terrapod/v1/agent-pools",
            json={"data": {"type": "agent-pools", "attributes": {"name": f"named-pool-{tag}"}}},
            headers=AUTH,
        )
        assert pool.status_code == 201, pool.text
        pool_id = pool.json()["data"]["id"]

        ws_id = await _ws(client, f"poolname-{tag}", **{"agent-pool-ids": [pool_id]})
        got = await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)
        attrs = got.json()["data"]["attributes"]

        assert attrs["agent-pool-ids"] == [pool_id]
        # Positional: the Nth name describes the Nth id.
        assert attrs["agent-pool-names"] == [f"named-pool-{tag}"], attrs["agent-pool-names"]
        # The singular back-compat projection still describes element 0.
        assert attrs["agent-pool-name"] == f"named-pool-{tag}"

    async def test_a_workspace_with_no_pool_gets_an_empty_list_not_null(self, app, client):
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"nopool-{tag}")
        attrs = (await client.get(f"/api/v2/workspaces/{ws_id}", headers=AUTH)).json()["data"][
            "attributes"
        ]
        assert attrs["agent-pool-names"] == []
        assert attrs["agent-pool-name"] is None


class TestAllAccessEndpointsResolve:
    """Only /workspaces/{id}/access was tested; the other four kinds resolve
    their model and id-prefix from a per-kind table (_ACCESS_KINDS). A wrong
    prefix 422s every valid id and a wrong axis routes catalog through the
    workspace clamp — both would ship silently."""

    async def _pool(self, client, tag):
        r = await client.post(
            "/api/terrapod/v1/agent-pools",
            json={"data": {"type": "agent-pools", "attributes": {"name": f"acc-pool-{tag}"}}},
            headers=AUTH,
        )
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"]

    async def _module(self, client, tag):
        r = await client.post(
            "/api/terrapod/v1/registry-modules",
            json={"data": {"attributes": {"name": f"accmod{tag}", "provider": "aws"}}},
            headers=AUTH,
        )
        assert r.status_code == 201, r.text
        return r.json()["data"]["id"]

    async def test_agent_pool_access_resolves(self, app, client):
        tag = uuid.uuid4().hex[:8]
        pid = await self._pool(client, tag)
        resp = await client.get(f"/api/terrapod/v1/agent-pools/{pid}/access", headers=AUTH)
        assert resp.status_code == 200, resp.text
        a = resp.json()["data"]["attributes"]
        assert a["resource"]["kind"] == "agent-pools"
        assert a["axis"] == "pool"
        assert "platform-admin" in a["platform-paths"]

    async def test_registry_module_access_resolves(self, app, client):
        tag = uuid.uuid4().hex[:8]
        mid = await self._module(client, tag)
        resp = await client.get(f"/api/terrapod/v1/registry-modules/{mid}/access", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["axis"] == "registry"

    async def test_a_bad_id_is_422_not_500(self, app, client):
        resp = await client.get("/api/terrapod/v1/agent-pools/not-a-uuid/access", headers=AUTH)
        assert resp.status_code == 422

    async def test_unknown_kind_is_404(self, app, client):
        resp = await client.get(
            f"/api/terrapod/v1/nonsense-kind/{uuid.uuid4()}/access", headers=AUTH
        )
        assert resp.status_code == 404


class TestAccessEndpointsAreGated:
    """The preview endpoints have 403 coverage; the five /access routes carry
    the same require_admin_or_audit gate and had none."""

    async def test_a_non_admin_is_refused_at_the_route(self, app, client):
        from tests.integration.conftest import regular_user, set_auth

        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"accgate-{tag}")
        set_auth(app, regular_user())
        try:
            resp = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/access", headers=AUTH)
            assert resp.status_code == 403, resp.text
        finally:
            set_auth(app, admin_user())


class TestCatalogAxisAndNotes:
    """The catalog axis and the per-entry notes were both untested. The catalog
    clamp in the PREVIEW is the security-relevant bit: if it were dropped, a
    role would be advertised as granting write on a catalog-managed workspace it
    cannot write — the 'view disagrees with enforcement' failure."""

    async def test_the_everyone_floor_note_appears_on_the_forward_view(self, app, client):
        tag = uuid.uuid4().hex[:8]
        await _ws(client, f"note-{tag}", labels={"grp": tag, "access": "everyone"})
        await _role(
            client,
            f"note-role-{tag}",
            **{"allow-labels": {"grp": tag}, "workspace-permission": "read"},
        )
        resp = await client.get(f"/api/terrapod/v1/roles/note-role-{tag}/preview", headers=AUTH)
        ws = resp.json()["data"]["attributes"]["axes"]["workspace"]["resources"][0]
        assert "everyone-floor" in ws["notes"], ws

    async def test_the_catalog_axis_is_present_in_the_breakdown(self, app, client):
        """Even with zero catalog items, the axis key must exist so a consumer
        reading axes['catalog'] does not KeyError."""
        tag = uuid.uuid4().hex[:8]
        await _role(
            client, f"cat-role-{tag}", **{"allow-labels": {"x": tag}, "catalog-permission": "use"}
        )
        resp = await client.get(f"/api/terrapod/v1/roles/cat-role-{tag}/preview", headers=AUTH)
        axes = resp.json()["data"]["attributes"]["axes"]
        assert "catalog" in axes
        assert axes["catalog"]["granted-count"] == 0
