"""Tests for agent pool heartbeat endpoint."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app

_BASE = "http://test"


def _mock_listener_dict(listener_id=None, name="listener-1", pool_id=None):
    """Return a dict matching the Redis-backed listener shape."""
    return {
        "id": str(listener_id or uuid.uuid4()),
        "name": name,
        "pool_id": str(pool_id or uuid.uuid4()),
        "status": "online",
        "capacity": "10",
        "active_runs": "0",
        "last_heartbeat": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
    }


class TestListenerHeartbeat:
    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.heartbeat_listener", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_sets_redis_keys(self, mock_get_listener, mock_heartbeat, mock_publish):
        lid = uuid.uuid4()
        listener = _mock_listener_dict(listener_id=lid)
        mock_get_listener.return_value = listener

        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{lid}/heartbeat",
                json={"capacity": 5, "active_runs": 2},
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

        # Verify heartbeat_listener was called with correct args
        mock_heartbeat.assert_called_once()
        call_kwargs = mock_heartbeat.call_args.kwargs
        assert call_kwargs["listener_id"] == str(lid)
        assert call_kwargs["name"] == "listener-1"
        assert call_kwargs["capacity"] == "5"
        assert call_kwargs["active_runs"] == "2"

    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.heartbeat_listener", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_publishes_admin_event(
        self, mock_get_listener, mock_heartbeat, mock_publish
    ):
        lid = uuid.uuid4()
        listener = _mock_listener_dict(listener_id=lid)
        mock_get_listener.return_value = listener

        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{lid}/heartbeat",
                json={"capacity": 3, "active_runs": 1},
            )

        assert res.status_code == 200

        # Verify publish_event was called with admin channel
        assert mock_publish.call_count >= 1
        channels = [call.args[0] for call in mock_publish.call_args_list]
        assert "tp:admin_events" in channels

    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_not_found(self, mock_get_listener):
        mock_get_listener.return_value = None

        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{uuid.uuid4()}/heartbeat",
                json={"capacity": 1},
            )

        assert res.status_code == 404
