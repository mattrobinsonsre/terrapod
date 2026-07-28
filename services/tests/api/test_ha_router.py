"""Tests for the HA whoami endpoint (#960 phase 1, #1101).

`whoami` is the mechanism DNS-derived leadership rests on: a node probes the
shared name and asks whoever answers who they are.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terrapod.api.routers.ha import router

pytestmark = pytest.mark.asyncio


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/terrapod/v1")
    return app


async def _get(path: str = "/api/terrapod/v1/ha/whoami"):
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


class TestWhoami:
    @patch("terrapod.services.ha_role.get_role", new_callable=AsyncMock)
    @patch("terrapod.services.ha_role.node_id")
    async def test_reports_identity_and_role(self, mock_node_id, mock_role):
        mock_node_id.return_value = "node-a"
        mock_role.return_value = "leader"

        resp = await _get()

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["node-id"] == "node-a"
        assert attrs["role"] == "leader"

    @patch("terrapod.services.ha_role.get_role", new_callable=AsyncMock)
    @patch("terrapod.services.ha_role.node_id")
    async def test_no_auth_required(self, mock_node_id, mock_role):
        """The probe runs before any trust exists between the two nodes."""
        mock_node_id.return_value = "node-a"
        mock_role.return_value = "follower"

        resp = await _get()

        assert resp.status_code == 200

    @patch("terrapod.services.ha_role.get_role", new_callable=AsyncMock)
    @patch("terrapod.services.ha_role.node_id")
    async def test_response_is_not_cacheable(self, mock_node_id, mock_role):
        """A cached answer would pin leadership to a stale node."""
        mock_node_id.return_value = "node-a"
        mock_role.return_value = "leader"

        resp = await _get()

        assert resp.headers["cache-control"] == "no-store"

    @patch("terrapod.services.ha_role.get_role", new_callable=AsyncMock)
    @patch("terrapod.services.ha_role.node_id")
    async def test_unnamed_node_still_answers(self, mock_node_id, mock_role):
        """A single-node install has no node name and must not 500 here."""
        mock_node_id.return_value = ""
        mock_role.return_value = "leader"

        resp = await _get()

        assert resp.status_code == 200
        assert resp.json()["data"]["id"] == "unnamed"
        assert resp.json()["data"]["attributes"]["node-id"] == ""
