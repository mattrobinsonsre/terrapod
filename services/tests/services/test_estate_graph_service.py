"""Tests for estate_graph_service — whole-estate topology derivation (#763)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import estate_graph_service

W1, W2, W3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()  # W3 is NOT visible
M1, M2 = uuid.uuid4(), uuid.uuid4()  # M2 links only to the hidden workspace
POOL = uuid.uuid4()


def _ws(wid, name, labels=None, pool=None, mode="agent"):
    m = MagicMock()
    m.id, m.name, m.labels, m.agent_pool_id, m.execution_mode = wid, name, labels or {}, pool, mode
    return m


def _res(rows):
    r = MagicMock()
    r.scalars.return_value.all.return_value = rows
    return r


def _named(**attrs):
    # MagicMock(name=...) sets the mock's *name*, not a .name attribute — assign
    # explicitly so pool/module `.name` reads return the real string.
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _mk_db(workspaces, pools, run_triggers, remote_states, links, modules):
    db = AsyncMock()
    # derive_estate_graph issues selects in this exact order:
    # Workspace, AgentPool, RunTrigger, WorkspaceRemoteStateConsumer,
    # ModuleWorkspaceLink, then RegistryModule (only if any module is used).
    seq = [_res(workspaces), _res(pools), _res(run_triggers), _res(remote_states), _res(links)]
    if modules:
        seq.append(_res(modules))
    db.execute.side_effect = seq
    return db


def _user():
    u = MagicMock()
    u.email = "t@example.com"
    return u


async def _derive(db, visible_ids):
    async def caps(_db, _user, ws):
        return {"run:read"} if ws.id in visible_ids else set()

    with patch.object(estate_graph_service, "resolve_workspace_capabilities_for", side_effect=caps):
        return await estate_graph_service.derive_estate_graph(db, _user())


class TestEstateGraph:
    async def test_rbac_filters_hidden_workspaces_and_their_edges(self):
        workspaces = [
            _ws(W1, "vpc-core", {"team": "platform"}, POOL),
            _ws(W2, "app-web", {"team": "web"}),
            _ws(W3, "secret-ws", {"team": "sec"}),  # hidden
        ]
        pools = [_named(id=POOL, name="aws-use1")]
        run_triggers = [
            MagicMock(source_workspace_id=W1, workspace_id=W2),  # both visible → kept
            MagicMock(source_workspace_id=W2, workspace_id=W3),  # touches hidden → dropped
        ]
        remote = [MagicMock(producer_workspace_id=W1, consumer_workspace_id=W2)]
        links = [
            MagicMock(module_id=M1, workspace_id=W1),  # visible → M1 included
            MagicMock(module_id=M2, workspace_id=W3),  # only hidden → M2 excluded
        ]
        modules = [_named(id=M1, name="vpc", provider="aws")]
        db = _mk_db(workspaces, pools, run_triggers, remote, links, modules)

        g = await _derive(db, {W1, W2})

        wnodes = {n["name"]: n for n in g["nodes"] if n["kind"] == "workspace"}
        mnodes = [n for n in g["nodes"] if n["kind"] == "module"]
        assert set(wnodes) == {"vpc-core", "app-web"}  # hidden one gone
        assert [m["name"] for m in mnodes] == ["vpc/aws"]  # M2 excluded

        kinds = {(e["source"], e["target"], e["kind"]) for e in g["edges"]}
        assert (f"ws-{W1}", f"ws-{W2}", "run-trigger") in kinds  # kept
        assert all(f"ws-{W3}" not in (e[0], e[1]) for e in kinds)  # nothing touches hidden
        assert (f"ws-{W2}", f"ws-{W1}", "remote-state") in kinds  # consumer -> producer
        assert (f"mod-{M1}", f"ws-{W1}", "uses-module") in kinds

        assert g["meta"]["counts"] == {"workspaces": 2, "modules": 1, "edges": 3}

    async def test_pool_and_indegree(self):
        workspaces = [_ws(W1, "hub", {}, POOL), _ws(W2, "leaf", {}, None, mode="local")]
        pools = [_named(id=POOL, name="aws-use1")]
        run_triggers = [MagicMock(source_workspace_id=W1, workspace_id=W2)]
        db = _mk_db(workspaces, pools, run_triggers, [], [], [])

        g = await _derive(db, {W1, W2})
        by = {n["name"]: n for n in g["nodes"]}
        assert by["hub"]["pool"] == "aws-use1"
        assert by["leaf"]["pool"] == "(local)"
        assert by["leaf"]["indeg"] == 1  # target of the run-trigger
        assert by["hub"]["indeg"] == 0

    async def test_empty_estate(self):
        db = _mk_db([], [], [], [], [], [])
        g = await _derive(db, set())
        assert g["nodes"] == [] and g["edges"] == []
        assert g["meta"]["counts"] == {"workspaces": 0, "modules": 0, "edges": 0}
