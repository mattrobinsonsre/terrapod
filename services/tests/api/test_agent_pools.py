"""Tests for agent pool heartbeat endpoint."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.db.session import get_db

_BASE = "http://test"


def _mock_listener(listener_id=None, name="listener-1", pool_id=None):
    listener = MagicMock()
    listener.id = listener_id or uuid.uuid4()
    listener.name = name
    listener.pool_id = pool_id or uuid.uuid4()
    listener.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return listener


class TestListenerHeartbeat:
    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_redis_client")
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_sets_redis_keys(
        self, mock_get_listener, mock_redis_client, mock_publish
    ):
        listener = _mock_listener()
        mock_get_listener.return_value = listener

        mock_redis = AsyncMock()
        mock_redis_client.return_value = mock_redis

        mock_db = AsyncMock()

        app = create_app()
        app.dependency_overrides[get_db] = lambda: mock_db

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE
        ) as client:
            res = await client.post(
                f"/api/v2/listeners/{listener.id}/heartbeat",
                json={"capacity": 5, "active_runs": 2, "runner_definitions": ["default"]},
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

        # Verify Redis setex calls were made
        prefix = f"tp:listener:{listener.id}"
        setex_keys = [call.args[0] for call in mock_redis.setex.call_args_list]
        assert f"{prefix}:status" in setex_keys
        assert f"{prefix}:heartbeat" in setex_keys
        assert f"{prefix}:capacity" in setex_keys
        assert f"{prefix}:active_runs" in setex_keys
        assert f"{prefix}:runner_defs" in setex_keys

    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_redis_client")
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_publishes_admin_event(
        self, mock_get_listener, mock_redis_client, mock_publish
    ):
        listener = _mock_listener()
        mock_get_listener.return_value = listener

        mock_redis = AsyncMock()
        mock_redis_client.return_value = mock_redis

        mock_db = AsyncMock()

        app = create_app()
        app.dependency_overrides[get_db] = lambda: mock_db

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE
        ) as client:
            res = await client.post(
                f"/api/v2/listeners/{listener.id}/heartbeat",
                json={"capacity": 3, "active_runs": 1},
            )

        assert res.status_code == 200

        # Verify publish_event was called with admin channel
        assert mock_publish.call_count >= 1
        channels = [call.args[0] for call in mock_publish.call_args_list]
        assert "tp:admin_events" in channels

    @patch("terrapod.redis.client.get_redis_client")
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_not_found(self, mock_get_listener, mock_redis_client):
        mock_get_listener.return_value = None

        mock_db = AsyncMock()

        app = create_app()
        app.dependency_overrides[get_db] = lambda: mock_db

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE
        ) as client:
            res = await client.post(
                f"/api/v2/listeners/{uuid.uuid4()}/heartbeat",
                json={"capacity": 1},
            )

        assert res.status_code == 404
