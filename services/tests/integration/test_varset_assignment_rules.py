"""Rule-based variable-set assignment and the association views (#1440).

Integration tier deliberately: a rule is evaluated by handing it to the same SQL
selector the bulk-update surface uses, so what needs proving is *which
workspaces it selects*. A mocked session could only show that a query was
built — the interesting part is what the database returns for it.
"""

import uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import VariableSet, VariableSetWorkspace, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import variable_service
from tests.integration.conftest import AUTH, admin_user, regular_user, set_auth

pytestmark = pytest.mark.integration


async def _seed(workspaces: list[tuple[str, dict]], **varset_kw) -> tuple[VariableSet, dict]:
    """A variable set plus labelled workspaces; returns the set and name→id."""
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        ids = {}
        for name, labels in workspaces:
            ws = Workspace(name=f"{name}-{tag}", labels=labels)
            db.add(ws)
            await db.flush()
            ids[name] = ws.id

        vs = VariableSet(name=f"vs-{tag}", **varset_kw)
        db.add(vs)
        await db.flush()
        await db.commit()
        return vs, ids


async def _reload(vs_id) -> VariableSet:
    async with get_db_session() as db:
        return (await db.execute(select(VariableSet).where(VariableSet.id == vs_id))).scalar_one()


@pytest.mark.asyncio
async def test_rule_selects_matching_workspaces_only(app):
    vs, ids = await _seed(
        [("prod-api", {"env": "prod"}), ("dev-api", {"env": "dev"})],
        assignment_rule={"labels": {"env": "prod"}},
    )
    async with get_db_session() as db:
        vs = await _reload(vs.id)
        rows = await variable_service.workspaces_for_varset(db, vs)
        assert [(w.id, s) for w, s in rows] == [(ids["prod-api"], variable_service.ASSIGNMENT_RULE)]


@pytest.mark.asyncio
async def test_the_two_views_agree(app):
    """The workspace-side and varset-side views must never contradict each other.

    They are the same question asked from opposite ends, so this asserts the
    round-trip rather than either view alone. If they can disagree, one of them
    is lying about who currently holds a credential.
    """
    vs, ids = await _seed(
        [("alpha", {"team": "core"}), ("beta", {"team": "core"}), ("gamma", {"team": "other"})],
        assignment_rule={"labels": {"team": "core"}},
    )
    async with get_db_session() as db:
        vs = await _reload(vs.id)
        from_varset = {w.id for w, _ in await variable_service.workspaces_for_varset(db, vs)}
        assert from_varset == {ids["alpha"], ids["beta"]}

        for name in ("alpha", "beta"):
            seen = {v.id for v, _ in await variable_service.applicable_varsets(db, ids[name])}
            assert vs.id in seen, f"{name}: workspace view lost a set the varset view claims"

        gamma = {v.id for v, _ in await variable_service.applicable_varsets(db, ids["gamma"])}
        assert vs.id not in gamma


@pytest.mark.asyncio
async def test_explicit_assignment_wins_the_label(app):
    """A set both explicitly assigned and rule-matched reports 'explicit'.

    The source label drives what the UI offers to edit, and the explicit
    assignment is the only part of it there is anything to remove.
    """
    vs, ids = await _seed([("both", {"env": "prod"})], assignment_rule={"labels": {"env": "prod"}})
    async with get_db_session() as db:
        db.add(VariableSetWorkspace(variable_set_id=vs.id, workspace_id=ids["both"]))
        await db.commit()

    async with get_db_session() as db:
        vs = await _reload(vs.id)
        rows = await variable_service.workspaces_for_varset(db, vs)
        assert [s for _, s in rows] == [variable_service.ASSIGNMENT_EXPLICIT]

        applies = await variable_service.applicable_varsets(db, ids["both"])
        assert [s for v, s in applies if v.id == vs.id] == [variable_service.ASSIGNMENT_EXPLICIT]


@pytest.mark.asyncio
async def test_unparseable_rule_matches_nothing(app):
    """A rule that no longer parses matches nothing, never everything.

    A filter key dropped in some later version must not silently turn a scoped
    credential set into an estate-wide one. Failing closed is the entire reason
    the parse is guarded.
    """
    vs, ids = await _seed([("some-ws", {"env": "prod"})], assignment_rule={"not-a-filter-key": "x"})
    async with get_db_session() as db:
        vs = await _reload(vs.id)
        assert await variable_service.workspaces_for_varset(db, vs) == []
        applies = await variable_service.applicable_varsets(db, ids["some-ws"])
        assert vs.id not in {v.id for v, _ in applies}


@pytest.mark.asyncio
async def test_rule_matched_set_is_actually_injected(app):
    """Resolution honours the rule, not merely the read-only views.

    A set that appears in the UI but never delivers its variables to the run
    would be worse than no feature at all.
    """
    vs, ids = await _seed(
        [("prod-x", {"env": "prod"})], assignment_rule={"labels": {"env": "prod"}}
    )
    async with get_db_session() as db:
        sets = await variable_service._get_applicable_varsets(db, ids["prod-x"], priority=False)
        assert vs.id in {s.id for s in sets}


@pytest.mark.asyncio
async def test_global_set_reports_every_workspace(app):
    """A global set applies everywhere, and says so.

    Reporting an empty list because nothing is explicitly assigned would be the
    exact opposite of the truth for the set with the widest reach.
    """
    vs, ids = await _seed([("g1", {}), ("g2", {})], global_set=True)
    async with get_db_session() as db:
        vs = await _reload(vs.id)
        rows = await variable_service.workspaces_for_varset(db, vs)
        by_id = {w.id: s for w, s in rows}
        for name in ("g1", "g2"):
            assert by_id.get(ids[name]) == variable_service.ASSIGNMENT_GLOBAL


# ---------------------------------------------------------------------------
# The HTTP surface: the endpoints and the update path.
#
# The service-level tests above prove the matching is right. These prove the
# router wired it up — including the update path, which carries the trickiest
# logic (it validates the *resulting* state, not just the patch) and is the one
# an operator actually drives from the UI.
# ---------------------------------------------------------------------------

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
VARSET_ENDPOINT = "/api/v2/organizations/default/varsets"


async def _ws(client, name: str, labels: dict | None = None) -> str:
    resp = await client.post(
        WS_ENDPOINT,
        json={"data": {"type": "workspaces", "attributes": {"name": name, "labels": labels or {}}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _vs(client, name: str, **attrs) -> str:
    resp = await client.post(
        VARSET_ENDPOINT,
        json={"data": {"type": "varsets", "attributes": {"name": name, **attrs}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


class TestAssociationEndpoints:
    async def test_workspace_view_reports_each_source(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"assoc-{tag}", {"assoc": tag})

        explicit = await _vs(client, f"explicit-{tag}")
        await client.post(
            f"/api/v2/varsets/{explicit}/relationships/workspaces",
            json={"data": [{"id": ws_id, "type": "workspaces"}]},
            headers=AUTH,
        )
        glob = await _vs(client, f"global-{tag}", **{"global": True})
        ruled = await _vs(client, f"ruled-{tag}", **{"assignment-rule": {"labels": {"assoc": tag}}})

        resp = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/varsets", headers=AUTH)
        assert resp.status_code == 200
        by_id = {v["id"]: v["attributes"]["assignment-source"] for v in resp.json()["data"]}
        assert by_id[explicit] == "explicit"
        assert by_id[glob] == "global"
        assert by_id[ruled] == "rule"

    async def test_varset_view_lists_the_workspaces_a_rule_reaches(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        hit = await _ws(client, f"hit-{tag}", {"blast": tag})
        miss = await _ws(client, f"miss-{tag}", {"blast": "other"})
        vs_id = await _vs(client, f"blast-{tag}", **{"assignment-rule": {"labels": {"blast": tag}}})

        resp = await client.get(
            f"/api/terrapod/v1/varsets/{vs_id}/relationships/workspaces", headers=AUTH
        )
        assert resp.status_code == 200
        ids = {w["id"]: w["attributes"]["assignment-source"] for w in resp.json()["data"]}
        assert ids == {hit: "rule"}
        assert miss not in ids

    async def test_workspace_view_404s_for_an_unknown_workspace(self, app, client):
        set_auth(app, admin_user())
        resp = await client.get(
            f"/api/terrapod/v1/workspaces/ws-{uuid.uuid4()}/varsets", headers=AUTH
        )
        assert resp.status_code == 404

    async def test_varset_view_404s_for_an_unknown_varset(self, app, client):
        set_auth(app, admin_user())
        resp = await client.get(
            f"/api/terrapod/v1/varsets/varset-{uuid.uuid4()}/relationships/workspaces",
            headers=AUTH,
        )
        assert resp.status_code == 404

    async def test_varset_view_is_admin_only(self, app, client):
        """The blast-radius view names every workspace a credential set reaches,
        so it carries the same admin gate as the rest of varset management."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        vs_id = await _vs(client, f"gated-{tag}")

        set_auth(app, regular_user())
        resp = await client.get(
            f"/api/terrapod/v1/varsets/{vs_id}/relationships/workspaces", headers=AUTH
        )
        assert resp.status_code in (401, 403)


class TestAssignmentRuleUpdates:
    async def test_a_rule_can_be_set_and_then_cleared(self, app, client):
        """Clearing must actually clear. Sending null has to remove the rule
        rather than be read as "leave it alone", or a set can never stop
        matching once it starts."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"upd-{tag}", {"upd": tag})
        vs_id = await _vs(client, f"upd-{tag}")

        patch = await client.patch(
            f"/api/v2/varsets/{vs_id}",
            json={
                "data": {
                    "type": "varsets",
                    "attributes": {"assignment-rule": {"labels": {"upd": tag}}},
                }
            },
            headers=AUTH,
        )
        assert patch.status_code == 200
        assert patch.json()["data"]["attributes"]["assignment-rule"] == {"labels": {"upd": tag}}

        applies = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/varsets", headers=AUTH)
        assert vs_id in {v["id"] for v in applies.json()["data"]}

        cleared = await client.patch(
            f"/api/v2/varsets/{vs_id}",
            json={"data": {"type": "varsets", "attributes": {"assignment-rule": None}}},
            headers=AUTH,
        )
        assert cleared.status_code == 200
        assert cleared.json()["data"]["attributes"]["assignment-rule"] is None

        applies = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/varsets", headers=AUTH)
        assert vs_id not in {v["id"] for v in applies.json()["data"]}

    async def test_an_invalid_rule_is_rejected_on_update_too(self, app, client):
        set_auth(app, admin_user())
        vs_id = await _vs(client, f"badupd-{uuid.uuid4().hex[:8]}")
        resp = await client.patch(
            f"/api/v2/varsets/{vs_id}",
            json={"data": {"type": "varsets", "attributes": {"assignment-rule": {"nope": 1}}}},
            headers=AUTH,
        )
        assert resp.status_code == 422

    async def test_making_a_ruled_set_global_is_rejected(self, app, client):
        """The contradiction can be reached without either field being invalid
        on its own — this PATCH only sets `global`, and the rule is already
        there. Validating the resulting state is what catches it."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        vs_id = await _vs(client, f"conflict-{tag}", **{"assignment-rule": {"labels": {"a": "b"}}})

        resp = await client.patch(
            f"/api/v2/varsets/{vs_id}",
            json={"data": {"type": "varsets", "attributes": {"global": True}}},
            headers=AUTH,
        )
        assert resp.status_code == 422

    async def test_a_falsy_all_is_never_stored(self, app, client):
        """`all` is stripped rather than stored, so the persisted shape is
        exactly the scoping dimensions — which is what lets the typed provider
        and the UI form model it completely without drift."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        vs_id = await _vs(
            client, f"falsyall-{tag}", **{"assignment-rule": {"labels": {"x": "y"}, "all": False}}
        )
        resp = await client.get(f"/api/v2/varsets/{vs_id}", headers=AUTH)
        assert resp.json()["data"]["attributes"]["assignment-rule"] == {"labels": {"x": "y"}}


class TestRuleMatchedSetsReachTheRun:
    async def test_a_rule_matched_set_contributes_its_variables(self, app, client):
        """The end that matters: a rule that shows up in the UI but whose
        variables never reach the run would be worse than no feature."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"resolve-{tag}", {"resolve": tag})
        vs_id = await _vs(
            client, f"resolve-{tag}", **{"assignment-rule": {"labels": {"resolve": tag}}}
        )
        await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {"key": "FROM_RULE", "value": "yes", "category": "env"},
                }
            },
            headers=AUTH,
        )

        from terrapod.db.session import get_db_session
        from terrapod.services import variable_service

        async with get_db_session() as db:
            resolved = await variable_service.resolve_variables(
                db, uuid.UUID(ws_id.removeprefix("ws-"))
            )
        by_key = {v.key: v for v in resolved}
        assert "FROM_RULE" in by_key, "a rule-matched set did not reach variable resolution"
        assert by_key["FROM_RULE"].value == "yes"
        assert by_key["FROM_RULE"].category == "env"

    async def test_workspace_variables_still_beat_a_rule_matched_set(self, app, client):
        """Precedence is unchanged by how a set arrived. A rule-matched set is an
        ordinary non-priority set, so a workspace variable still wins."""
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        ws_id = await _ws(client, f"prec-{tag}", {"prec": tag})
        vs_id = await _vs(client, f"prec-{tag}", **{"assignment-rule": {"labels": {"prec": tag}}})
        await client.post(
            f"/api/v2/varsets/{vs_id}/relationships/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {"key": "WHO_WINS", "value": "varset", "category": "env"},
                }
            },
            headers=AUTH,
        )
        await client.post(
            f"/api/v2/workspaces/{ws_id}/vars",
            json={
                "data": {
                    "type": "vars",
                    "attributes": {"key": "WHO_WINS", "value": "workspace", "category": "env"},
                }
            },
            headers=AUTH,
        )

        from terrapod.db.session import get_db_session
        from terrapod.services import variable_service

        async with get_db_session() as db:
            resolved = await variable_service.resolve_variables(
                db, uuid.UUID(ws_id.removeprefix("ws-"))
            )
        by_key = {v.key: v for v in resolved}
        assert by_key["WHO_WINS"].value == "workspace"
