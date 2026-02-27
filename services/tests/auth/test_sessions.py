"""Tests for Redis-backed session management."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.auth.sessions import (
    SESSION_PREFIX,
    USER_SESSIONS_PREFIX,
    Session,
    _should_refresh_session,
    create_session,
    get_session,
    revoke_session,
)


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    redis = AsyncMock()
    pipe = AsyncMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    redis.pipeline.return_value = pipe
    return redis, pipe


class TestCreateSession:
    @patch("terrapod.auth.sessions.get_redis_client")
    async def test_create_session_returns_session_with_token(self, mock_get_redis, mock_redis):
        redis, pipe = mock_redis
        mock_get_redis.return_value = redis
        pipe.execute.return_value = [True, 1, True]

        session = await create_session(
            email="test@example.com",
            display_name="Test User",
            roles=["admin"],
            provider_name="local",
        )

        assert session.email == "test@example.com"
        assert session.display_name == "Test User"
        assert session.roles == ["admin"]
        assert session.provider_name == "local"
        assert session.token != ""
        assert len(session.token) > 20

    @patch("terrapod.auth.sessions.get_redis_client")
    async def test_create_session_stores_in_redis(self, mock_get_redis, mock_redis):
        redis, pipe = mock_redis
        mock_get_redis.return_value = redis
        pipe.execute.return_value = [True, 1, True]

        session = await create_session(
            email="test@example.com",
            display_name=None,
            roles=[],
            provider_name="oidc",
        )

        # Verify pipeline commands were called
        pipe.set.assert_called_once()
        call_args = pipe.set.call_args
        assert call_args[0][0] == SESSION_PREFIX + session.token

        pipe.sadd.assert_called_once()
        sadd_args = pipe.sadd.call_args
        assert sadd_args[0][0] == USER_SESSIONS_PREFIX + "test@example.com"

    @patch("terrapod.auth.sessions.get_redis_client")
    async def test_create_session_caps_ttl(self, mock_get_redis, mock_redis):
        redis, pipe = mock_redis
        mock_get_redis.return_value = redis
        pipe.execute.return_value = [True, 1, True]

        session = await create_session(
            email="test@example.com",
            display_name=None,
            roles=[],
            provider_name="oidc",
            max_ttl=3600,  # 1 hour cap
        )

        # The set call should use the capped TTL
        call_args = pipe.set.call_args
        assert call_args[1]["ex"] == 3600


class TestGetSession:
    @patch("terrapod.auth.sessions.get_redis_client")
    async def test_get_existing_session(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        session_data = {
            "email": "test@example.com",
            "display_name": "Test",
            "roles": ["admin"],
            "provider_name": "local",
            "created_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2026-01-01T12:00:00+00:00",
            "last_active_at": "2026-01-01T00:00:00+00:00",
        }
        redis.get.return_value = json.dumps(session_data)

        session = await get_session("test-token")

        assert session is not None
        assert session.email == "test@example.com"
        assert session.token == "test-token"
        assert session.roles == ["admin"]

    @patch("terrapod.auth.sessions.get_redis_client")
    async def test_get_nonexistent_session(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.get.return_value = None

        session = await get_session("nonexistent-token")
        assert session is None


class TestRevokeSession:
    @patch("terrapod.auth.sessions.get_redis_client")
    async def test_revoke_existing_session(self, mock_get_redis, mock_redis):
        redis, pipe = mock_redis
        mock_get_redis.return_value = redis

        session_data = json.dumps({"email": "test@example.com"})
        redis.get.return_value = session_data
        pipe.execute.return_value = [1, 1]

        result = await revoke_session("test-token")
        assert result is True

    @patch("terrapod.auth.sessions.get_redis_client")
    async def test_revoke_nonexistent_session(self, mock_get_redis, mock_redis):
        redis, pipe = mock_redis
        mock_get_redis.return_value = redis
        redis.get.return_value = None
        pipe.execute.return_value = [0]

        result = await revoke_session("nonexistent-token")
        assert result is False


class TestShouldRefreshSession:
    def test_should_refresh_after_interval(self):
        from datetime import UTC, datetime, timedelta

        old_time = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        session = Session(
            email="test@example.com",
            display_name=None,
            roles=[],
            provider_name="local",
            created_at=old_time,
            expires_at=old_time,
            last_active_at=old_time,
        )
        assert _should_refresh_session(session) is True

    def test_should_not_refresh_recently_active(self):
        from datetime import UTC, datetime

        recent_time = datetime.now(UTC).isoformat()
        session = Session(
            email="test@example.com",
            display_name=None,
            roles=[],
            provider_name="local",
            created_at=recent_time,
            expires_at=recent_time,
            last_active_at=recent_time,
        )
        assert _should_refresh_session(session) is False
