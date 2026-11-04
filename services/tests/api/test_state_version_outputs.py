"""The TFE state-version-output routes (#2376).

`tofu output` exited 1 with `could not read state version outputs: resource not
found` until these existed, because `GetRootOutputValues` has no fallback on
error. The data was already served natively; only the TFE-shaped door was
missing.

The blob path -- storage, decryption, typed reading -- is covered in
`tests/services/test_typed_state_outputs.py`. What is covered here is the two
things only a route can answer: the GATE, and the document shape.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import caps_for_level
from terrapod.services.state_graph_service import StateOutput

from .test_runs import _AUTH, _BASE, _make_app, _mock_workspace, _user

OUTS = [
    StateOutput(
        name="greeting", value="hello", sensitive=False, detailed_type="string", type="string"
    ),
    StateOutput(
        name="token", value="s3cret", sensitive=True, detailed_type="string", type="string"
    ),
]


def _sv(ws_id):
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.workspace_id = ws_id
    sv.serial = 1
    sv.state_size = 1024
    return sv


def _app_with(ws, sv):
    app, mock_db = _make_app(_user())
    result = MagicMock()
    result.scalar_one_or_none.return_value = sv
    mock_db.execute.return_value = result
    mock_db.get = AsyncMock(return_value=ws)
    return app


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@patch("terrapod.api.routers.tfe_v2._typed_outputs_of", new_callable=AsyncMock, return_value=OUTS)
@patch(
    "terrapod.api.routers.tfe_v2._runner_state_read_allowed",
    new_callable=AsyncMock,
    return_value=False,
)
class TestTheCollection:
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_a_READ_tier_user_gets_their_outputs(self, mock_ws, mock_caps, *_m):
        """The reason this gates on `state:read-metadata` and not `state:read`.

        `state:read` is a PLAN-tier capability, so gating the collection on it
        would 403 `tofu output` for every read-only user -- including for
        outputs that are not secret at all.
        """
        ws = _mock_workspace()
        mock_ws.return_value = ws
        mock_caps.return_value = caps_for_level("read")

        async with AsyncClient(
            transport=ASGITransport(app=_app_with(ws, _sv(ws.id))), base_url=_BASE
        ) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/current-state-version-outputs", headers=_AUTH
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert {o["attributes"]["name"] for o in body["data"]} == {"greeting", "token"}
        assert "pagination" in body["meta"]

    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_a_sensitive_value_is_WITHHELD_here(self, mock_ws, mock_caps, *_m):
        """Omitted, not masked.

        go-tfe types `value` as `any`, so a placeholder string would be
        indistinguishable from a real value to a client. The CLI reads a
        sensitive value through the per-output route, so absence is the honest
        answer and costs it nothing.
        """
        ws = _mock_workspace()
        mock_ws.return_value = ws
        mock_caps.return_value = caps_for_level("read")

        async with AsyncClient(
            transport=ASGITransport(app=_app_with(ws, _sv(ws.id))), base_url=_BASE
        ) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/current-state-version-outputs", headers=_AUTH
            )
        by_name = {o["attributes"]["name"]: o["attributes"] for o in resp.json()["data"]}
        assert by_name["greeting"]["value"] == "hello"
        assert "value" not in by_name["token"]
        assert by_name["token"]["sensitive"] is True
        assert "s3cret" not in resp.text

    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_every_item_carries_an_id_the_read_route_accepts(self, mock_ws, mock_caps, *_m):
        """go-tfe's parser silently DROPS an entry with no id.

        Which here would make `tofu output` conclude the workspace has no
        outputs -- the same hazard recorded on `list_workspace_tag_bindings`.
        And unlike that one, this id has to round-trip.
        """
        from terrapod.api.routers.tfe_v2 import _parse_output_id

        ws = _mock_workspace()
        mock_ws.return_value = ws
        mock_caps.return_value = caps_for_level("read")
        sv = _sv(ws.id)

        async with AsyncClient(transport=ASGITransport(app=_app_with(ws, sv)), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/current-state-version-outputs", headers=_AUTH
            )
        for item in resp.json()["data"]:
            assert item["id"].startswith("wsout-")
            sv_part, name = _parse_output_id(item["id"])
            assert sv_part == str(sv.id)
            assert name == item["attributes"]["name"]

    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_no_read_capability_is_a_403(self, mock_ws, mock_caps, *_m):
        ws = _mock_workspace()
        mock_ws.return_value = ws
        mock_caps.return_value = set()

        async with AsyncClient(
            transport=ASGITransport(app=_app_with(ws, _sv(ws.id))), base_url=_BASE
        ) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/current-state-version-outputs", headers=_AUTH
            )
        assert resp.status_code == 403, resp.text


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@patch("terrapod.api.routers.tfe_v2._typed_outputs_of", new_callable=AsyncMock, return_value=OUTS)
class TestTheRunnerPathMirrorsTheSiblingRoute:
    """A run already permitted to read a producer's whole state (#344) may read
    its outputs. Refusing here while `current-state-version` allows it would be
    inconsistent in the strict direction with nothing in the code to explain it."""

    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch(
        "terrapod.api.routers.tfe_v2._runner_state_read_allowed",
        new_callable=AsyncMock,
        return_value=True,
    )
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_an_allowlisted_runner_is_admitted_without_workspace_caps(
        self, mock_ws, _allowed, mock_caps, *_m
    ):
        ws = _mock_workspace()
        mock_ws.return_value = ws
        mock_caps.return_value = set()  # no workspace capability at all

        async with AsyncClient(
            transport=ASGITransport(app=_app_with(ws, _sv(ws.id))), base_url=_BASE
        ) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/current-state-version-outputs", headers=_AUTH
            )
        assert resp.status_code == 200, resp.text
        mock_caps.assert_not_called()


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@patch("terrapod.api.routers.tfe_v2._typed_outputs_of", new_callable=AsyncMock, return_value=OUTS)
class TestThePerOutputRead:
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.tfe_v2._load_state_version_scoped")
    async def test_it_REVEALS_a_sensitive_value(self, mock_load, mock_caps, *_m):
        """The route that makes `tofu output` work on a sensitive output.

        Masking here was the alternative and it is simply a wrong answer: the
        CLI fetches a sensitive value only through this route.
        """
        ws = _mock_workspace()
        sv = _sv(ws.id)
        mock_load.return_value = sv
        mock_caps.return_value = caps_for_level("plan")
        assert cap.STATE_READ in caps_for_level("plan")

        async with AsyncClient(transport=ASGITransport(app=_app_with(ws, sv)), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/state-version-outputs/wsout-{sv.id}:token", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["value"] == "s3cret"

    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.tfe_v2._load_state_version_scoped")
    async def test_a_READ_tier_user_is_refused_here(self, mock_load, mock_caps, *_m):
        """The asymmetry that is the whole design: read tier lists, plan tier
        reveals. `state:read` is state-grade (GHSA-gwwq-5v7q-h3f4)."""
        ws = _mock_workspace()
        sv = _sv(ws.id)
        mock_load.return_value = sv
        mock_caps.return_value = caps_for_level("read")
        assert cap.STATE_READ not in caps_for_level("read")

        async with AsyncClient(transport=ASGITransport(app=_app_with(ws, sv)), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/state-version-outputs/wsout-{sv.id}:token", headers=_AUTH)
        assert resp.status_code == 403, resp.text
        assert "s3cret" not in resp.text

    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
    @patch("terrapod.api.routers.tfe_v2._load_state_version_scoped")
    async def test_an_unknown_output_name_is_a_404(self, mock_load, mock_caps, *_m):
        ws = _mock_workspace()
        sv = _sv(ws.id)
        mock_load.return_value = sv
        mock_caps.return_value = caps_for_level("plan")

        async with AsyncClient(transport=ASGITransport(app=_app_with(ws, sv)), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/state-version-outputs/wsout-{sv.id}:nope", headers=_AUTH)
        assert resp.status_code == 404, resp.text


class TestTheSynthesisedId:
    @pytest.mark.parametrize(
        "bad",
        [
            "wsout-",  # nothing at all
            "wsout-not-a-uuid:name",  # left half is not a state version
            f"wsout-{uuid.uuid4()}",  # no separator, so no name
            f"wsout-{uuid.uuid4()}:",  # separator, empty name
            f"wsout-{uuid.uuid4()}:a/b",  # go-tfe refuses a slash client-side
            f"wsout-{uuid.uuid4()}:a b",  # ...and whitespace
        ],
    )
    def test_a_malformed_id_is_a_404_not_a_500(self, bad):
        """A bare `uuid.UUID()` on request input answers 500, which is what
        `test_id_prefix_tolerance` exists to forbid."""
        from fastapi import HTTPException

        from terrapod.api.routers.tfe_v2 import _parse_output_id

        with pytest.raises(HTTPException) as exc:
            _parse_output_id(bad)
        assert exc.value.status_code == 404

    def test_a_name_with_a_colon_keeps_everything_after_the_first_one(self):
        """`partition`, not `split`: a name is not guaranteed colon-free, and
        losing the tail would silently address a different output."""
        from terrapod.api.routers.tfe_v2 import _output_id, _parse_output_id

        sv_id = uuid.uuid4()
        sv_part, name = _parse_output_id(_output_id(sv_id, "a:b"))
        assert (sv_part, name) == (str(sv_id), "a:b")
