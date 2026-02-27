"""Tests for recent user tracking in Redis."""

import json
from unittest.mock import AsyncMock, patch

from terrapod.auth.recent_users import (
    RECENT_USER_PREFIX,
    RECENT_USER_TTL,
    list_recent_users,
    record_recent_user,
)


class TestRecordRecentUser:
    @patch("terrapod.auth.recent_users.get_redis_client")
    async def test_record_stores_with_ttl(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        await record_recent_user("oidc", "test@example.com", "Test User")

        redis.set.assert_called_once()
        call_args = redis.set.call_args
        key = call_args[0][0]
        assert key == f"{RECENT_USER_PREFIX}oidc:test@example.com"
        assert call_args[1]["ex"] == RECENT_USER_TTL

        stored_data = json.loads(call_args[0][1])
        assert stored_data["provider_name"] == "oidc"
        assert stored_data["email"] == "test@example.com"
        assert stored_data["display_name"] == "Test User"
        assert "last_seen" in stored_data

    @patch("terrapod.auth.recent_users.get_redis_client")
    async def test_record_with_null_display_name(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        await record_recent_user("local", "test@example.com", None)

        stored_data = json.loads(redis.set.call_args[0][1])
        assert stored_data["display_name"] is None


class TestListRecentUsers:
    @patch("terrapod.auth.recent_users.get_redis_client")
    async def test_list_returns_sorted_by_last_seen(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        # Simulate scan_iter returning keys
        keys = [
            f"{RECENT_USER_PREFIX}oidc:older@example.com",
            f"{RECENT_USER_PREFIX}oidc:newer@example.com",
        ]

        async def mock_scan_iter(**kwargs):
            for k in keys:
                yield k

        redis.scan_iter = mock_scan_iter

        # Mock get() for each key
        async def mock_get(key):
            if "older" in key:
                return json.dumps(
                    {
                        "provider_name": "oidc",
                        "email": "older@example.com",
                        "display_name": "Older",
                        "last_seen": "2026-01-01T00:00:00+00:00",
                    }
                )
            return json.dumps(
                {
                    "provider_name": "oidc",
                    "email": "newer@example.com",
                    "display_name": "Newer",
                    "last_seen": "2026-01-02T00:00:00+00:00",
                }
            )

        redis.get = mock_get

        users = await list_recent_users()

        assert len(users) == 2
        # Newest first
        assert users[0].email == "newer@example.com"
        assert users[1].email == "older@example.com"

    @patch("terrapod.auth.recent_users.get_redis_client")
    async def test_list_skips_expired_keys(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        async def mock_scan_iter(**kwargs):
            yield f"{RECENT_USER_PREFIX}oidc:gone@example.com"

        redis.scan_iter = mock_scan_iter
        redis.get = AsyncMock(return_value=None)

        users = await list_recent_users()
        assert users == []
