"""A workspace cannot be shaped to pull in a variable set (GHSA-49q6-pm68-3xgw).

Integration tier, driving the real routes, because that is what the finding is: the
reporter created a workspace through the API as a non-admin and watched another
team's rule-assigned secret set become applicable to it. The guard's mechanism is
"flush the pending row, then ask the real SQL selector about it", so a mocked
session could not exercise it and a fake answering from a fixture would be testing
the fixture.
"""

import uuid

import pytest

from tests.integration.conftest import AUTH, admin_user, regular_user, set_auth

pytestmark = pytest.mark.integration

WS = "/api/v2/organizations/default/workspaces"
VARSETS = "/api/v2/organizations/default/varsets"


async def _varset(client, name, *, rule=None, global_set=False):
    attrs = {"name": name, "global": global_set}
    if rule is not None:
        attrs["assignment-rule"] = rule
    resp = await client.post(
        VARSETS, json={"data": {"type": "varsets", "attributes": attrs}}, headers=AUTH
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]["id"]


async def _create_ws(client, name, **attrs):
    return await client.post(
        WS,
        json={"data": {"type": "workspaces", "attributes": {"name": name, **attrs}}},
        headers=AUTH,
    )


class TestTheReportedEscalation:
    async def test_a_non_admin_cannot_create_a_workspace_that_matches_the_rule(self, app, client):
        """The reporter's proof of concept, as a test: admin makes a rule-scoped
        set, a non-admin creates a workspace carrying the matching label."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"probe-{tag}", labels={"team": tag})

        assert resp.status_code == 403, resp.text
        # Name the set, so the operator knows what to ask about.
        assert f"teamA-{tag}" in resp.text
        assert "GHSA-49q6" in resp.text

    async def test_and_the_workspace_is_not_left_behind(self, app, client):
        """The refusal happens after a flush, so a rollback that did not fire would
        leave a committed workspace behind and the next create would 409 on the
        name — a refusal that still half-succeeded."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        assert (await _create_ws(client, f"probe-{tag}", labels={"team": tag})).status_code == 403

        # the name is free, which is only true if nothing was committed
        set_auth(app, admin_user())
        resp = await _create_ws(client, f"probe-{tag}", labels={"other": "x"})
        assert resp.status_code == 201, resp.text

    async def test_a_non_admin_cannot_patch_a_workspace_into_the_rule(self, app, client):
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        user = regular_user(f"probe-{tag}@test.com")
        set_auth(app, user)
        resp = await _create_ws(client, f"probe-{tag}", labels={})
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"labels": {"team": tag}}}},
            headers=AUTH,
        )
        assert resp.status_code == 403, resp.text
        assert f"teamA-{tag}" in resp.text

    async def test_a_platform_admin_may_do_both(self, app, client):
        """An admin already reads every variable set, so there is nothing to
        escalate to — and refusing them would make the feature unusable, since an
        admin is the only principal who can set it up at all."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        resp = await _create_ws(client, f"adminws-{tag}", labels={"team": tag})
        assert resp.status_code == 201, resp.text

        resp2 = await _create_ws(client, f"adminws2-{tag}", labels={})
        ws_id = resp2.json()["data"]["id"]
        resp3 = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"labels": {"team": tag}}}},
            headers=AUTH,
        )
        assert resp3.status_code == 200, resp3.text


class TestWhatMustSTILLBeAllowed:
    """A guard that refuses ordinary edits gets switched off, so these carry as
    much weight as the refusals."""

    async def test_an_ordinary_create_with_no_matching_rule(self, app, client):
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"mine-{tag}", labels={"team": "something-else"})
        assert resp.status_code == 201, resp.text

    async def test_an_unrelated_patch_on_a_workspace_that_already_matches(self, app, client):
        """The comparison is growth, not presence. A workspace an admin legitimately
        put in the set must stay editable by whoever administers it."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})
        resp = await _create_ws(client, f"blessed-{tag}", labels={"team": tag})
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["data"]["id"]

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        # a non-admin with admin ON the workspace via the everyone/owner path is
        # not modelled here; the point is that the GUARD does not object, so drive
        # it as the admin-created owner would be.
        set_auth(app, admin_user())
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"description": "unrelated"}}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text

    async def test_dropping_a_label_that_came_to_pull_a_set_in(self, app, client):
        """Shrinking is a de-escalation and must not need an admin.

        The realistic way a non-admin's own workspace comes to match is ORDER: they
        create it with a label, and an admin later writes a rule that happens to
        select it. Refusing the owner's attempt to drop that label would leave them
        unable to get out of a set they never opted into — the guard holding the
        workspace in the very state it exists to prevent.
        """
        tag = uuid.uuid4().hex[:8]
        user = regular_user(f"probe-{tag}@test.com")

        # 1. the workspace exists first, with the label, and nothing matches it
        set_auth(app, user)
        resp = await _create_ws(client, f"mine-{tag}", labels={"team": tag})
        assert resp.status_code == 201, resp.text
        ws_id = resp.json()["data"]["id"]

        # 2. an admin then writes a rule that selects it
        set_auth(app, admin_user())
        await _varset(client, f"teamA-{tag}", rule={"labels": {"team": tag}})

        # 3. the owner drops the label: strictly fewer sets reach the workspace
        set_auth(app, user)
        resp = await client.patch(
            f"/api/v2/workspaces/{ws_id}",
            json={"data": {"attributes": {"labels": {}}}},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text

    async def test_a_global_set_is_not_growth(self, app, client):
        """A global set already reaches every workspace, so counting it would refuse
        the first workspace anyone creates."""
        tag = uuid.uuid4().hex[:8]
        set_auth(app, admin_user())
        await _varset(client, f"everywhere-{tag}", global_set=True)

        set_auth(app, regular_user(f"probe-{tag}@test.com"))
        resp = await _create_ws(client, f"mine-{tag}")
        assert resp.status_code == 201, resp.text


class TestTheGuardIsWiredIntoBothPaths:
    """The service is inert unless the routers call it, and create and PATCH are
    separate call sites. The reporter's proof of concept used create, so gating
    only PATCH would leave the demonstrated attack working."""

    def test_create_and_patch_both_consult_it(self):
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2)
        assert src.count("refuse_varset_growth(") >= 2, (
            "only one of create/PATCH consults the guard"
        )
        assert "before=set()" in src, "create does not treat a new workspace as starting empty"
        assert "before=_varsets_before_patch" in src, (
            "PATCH does not compare against a pre-change snapshot"
        )

    def test_the_snapshot_is_taken_before_any_attribute_is_applied(self):
        """Taken after an attribute moved, the comparison straddles nothing and the
        guard permits exactly the edit it exists to refuse."""
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2.update_workspace)
        snap = src.index("_varsets_before_patch = ")
        writes = [src.index(m) for m in ("ws.name = ", "ws.description = ") if m in src]
        assert writes, "no attribute writes found — this test is stale"
        assert snap < min(writes), (
            "the snapshot is taken after an attribute has already been applied"
        )
