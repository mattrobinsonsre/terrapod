"""Tests for state_graph_service — single-workspace state resource graph (#765)."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.services import state_graph_service as svc

WS = uuid.uuid4()
SV_CUR, SV_OLD = uuid.uuid4(), uuid.uuid4()


# --- Pure transform (build_graph_from_state) --------------------------------


def _state(resources):
    return {"version": 4, "serial": 7, "resources": resources}


class TestBuildGraph:
    def test_addresses_edges_indegree_and_grouping_fields(self):
        state = _state(
            [
                {
                    "mode": "managed",
                    "type": "aws_vpc",
                    "name": "main",
                    "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                    "instances": [{"attributes": {}, "dependencies": []}],
                },
                {
                    "module": "module.net",
                    "mode": "managed",
                    "type": "aws_subnet",
                    "name": "a",
                    "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                    # count/for_each fan-out: two instances, deps unioned
                    "instances": [
                        {"index_key": 0, "dependencies": ["aws_vpc.main"]},
                        {"index_key": 1, "dependencies": ["aws_vpc.main"]},
                    ],
                },
                {
                    "mode": "data",
                    "type": "aws_ami",
                    "name": "ubuntu",
                    "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                    "instances": [{"dependencies": []}],
                },
            ]
        )
        g = svc.build_graph_from_state(state)
        nodes = {n["id"]: n for n in g["nodes"]}
        assert set(nodes) == {"aws_vpc.main", "module.net.aws_subnet.a", "data.aws_ami.ubuntu"}
        # data source address form
        assert nodes["data.aws_ami.ubuntu"]["mode"] == "data"
        # module prefix + grouping fields
        sub = nodes["module.net.aws_subnet.a"]
        assert sub["module"] == "module.net"
        assert sub["type"] == "aws_subnet"
        assert sub["provider"] == "aws"
        # count/for_each instance count → drawn as a nucleus of N pearls (#770)
        assert sub["instances"] == 2
        assert nodes["aws_vpc.main"]["instances"] == 1
        # root module is "" (impact-graph convention; the UI renders "(root)")
        assert nodes["aws_vpc.main"]["module"] == ""

        # one depends-on edge (deduped across the two subnet instances)
        assert g["edges"] == [
            {"source": "module.net.aws_subnet.a", "target": "aws_vpc.main", "kind": "depends-on"}
        ]
        # indegree accrues on the depended-upon node (the hub)
        assert nodes["aws_vpc.main"]["indeg"] == 1
        assert sub["indeg"] == 0
        assert g["meta"]["counts"] == {"resources": 3, "edges": 1}

    def test_dependency_on_unknown_resource_is_dropped(self):
        state = _state(
            [
                {
                    "mode": "managed",
                    "type": "aws_instance",
                    "name": "web",
                    "instances": [{"dependencies": ["aws_vpc.gone"]}],
                }
            ]
        )
        g = svc.build_graph_from_state(state)
        assert g["nodes"][0]["id"] == "aws_instance.web"
        assert g["edges"] == []  # target not a node → no dangling edge

    def test_empty_and_missing_resources(self):
        assert svc.build_graph_from_state({}) == svc.build_graph_from_state(_state([]))
        g = svc.build_graph_from_state(_state([]))
        assert g["nodes"] == [] and g["edges"] == []
        assert g["meta"]["truncated"] is False

    def test_truncation_reports_honestly(self):
        many = [
            {"mode": "managed", "type": "null_resource", "name": f"r{i}", "instances": []}
            for i in range(svc.MAX_NODES + 5)
        ]
        g = svc.build_graph_from_state(_state(many))
        assert len(g["nodes"]) == svc.MAX_NODES
        assert g["meta"]["truncated"] is True
        assert g["meta"]["total_resources"] == svc.MAX_NODES + 5


# --- derive_state_graph (RBAC + version selection + I/O) ---------------------


def _sv(sid, serial, size=100):
    m = MagicMock()
    m.id = sid
    m.workspace_id = WS
    m.serial = serial
    m.state_size = size
    m.created_at = datetime(2026, 1, serial, tzinfo=UTC)
    return m


def _db_with_versions(versions):
    db = AsyncMock()
    res = MagicMock()
    res.scalars.return_value.all.return_value = versions
    db.execute.return_value = res
    return db


def _user():
    u = MagicMock()
    u.email = "t@example.com"
    return u


_STATE_BYTES = json.dumps(
    _state(
        [
            {"mode": "managed", "type": "aws_vpc", "name": "main", "instances": []},
            {
                "mode": "managed",
                "type": "aws_subnet",
                "name": "a",
                "instances": [{"dependencies": ["aws_vpc.main"]}],
            },
        ]
    )
).encode()


def _patches(caps, versions, blob=_STATE_BYTES):
    ws = MagicMock()
    ws.id = WS
    st = MagicMock()
    st.get = AsyncMock(return_value=blob)
    return [
        patch("terrapod.api.routers.tfe_v2._get_workspace_by_id", AsyncMock(return_value=ws)),
        patch.object(svc, "resolve_workspace_capabilities_for", AsyncMock(return_value=caps)),
        patch.object(svc, "get_storage", return_value=st),
        patch("terrapod.crypto.state.decrypt_state_bytes", AsyncMock(side_effect=lambda b: b)),
    ]


async def _run(caps, versions, state_version_id=None, blob=_STATE_BYTES):
    db = _db_with_versions(versions)
    ps = _patches(caps, versions, blob)
    for p in ps:
        p.start()
    try:
        return await svc.derive_state_graph(db, _user(), f"ws-{WS}", state_version_id)
    finally:
        for p in ps:
            p.stop()


class TestDeriveStateGraph:
    async def test_requires_state_read(self):
        with pytest.raises(HTTPException) as ei:
            await _run({"state:read-metadata"}, [_sv(SV_CUR, 2)])
        assert ei.value.status_code == 403

    async def test_defaults_to_current_version_and_parses(self):
        g = await _run({"state:read"}, [_sv(SV_CUR, 2), _sv(SV_OLD, 1)])
        ids = {n["id"] for n in g["nodes"]}
        assert ids == {"aws_vpc.main", "aws_subnet.a"}
        assert g["meta"]["state_version"]["id"] == f"sv-{SV_CUR}"
        assert g["meta"]["state_version"]["is_current"] is True
        # picker list is newest-first, current flagged
        assert [v["id"] for v in g["meta"]["versions"]] == [f"sv-{SV_CUR}", f"sv-{SV_OLD}"]

    async def test_selects_requested_older_version(self):
        g = await _run(
            {"state:read"}, [_sv(SV_CUR, 2), _sv(SV_OLD, 1)], state_version_id=f"sv-{SV_OLD}"
        )
        assert g["meta"]["state_version"]["id"] == f"sv-{SV_OLD}"
        assert g["meta"]["state_version"]["is_current"] is False

    async def test_unknown_version_is_404(self):
        with pytest.raises(HTTPException) as ei:
            await _run({"state:read"}, [_sv(SV_CUR, 2)], state_version_id=f"sv-{uuid.uuid4()}")
        assert ei.value.status_code == 404

    async def test_no_versions_returns_empty(self):
        g = await _run({"state:read"}, [])
        assert g["nodes"] == [] and g["meta"]["versions"] == []
        assert g["meta"]["state_version"] is None

    async def test_uncommitted_content_returns_empty(self):
        # version row exists but /content PUT never landed (state_size == 0)
        g = await _run({"state:read"}, [_sv(SV_CUR, 1, size=0)])
        assert g["nodes"] == []
        assert g["meta"]["state_version"]["id"] == f"sv-{SV_CUR}"
