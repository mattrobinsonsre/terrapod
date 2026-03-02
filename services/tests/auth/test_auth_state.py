"""Tests for Redis-backed ephemeral auth state."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.auth.auth_state import (
    AUTH_CODE_PREFIX,
    AUTH_CODE_TTL,
    AUTH_STATE_PREFIX,
    AUTH_STATE_TTL,
    AuthCode,
    AuthState,
    consume_auth_code,
    consume_auth_state,
    generate_code,
    generate_state,
    store_auth_code,
    store_auth_state,
)


class TestGenerators:
    def test_generate_state_is_unique(self):
        s1 = generate_state()
        s2 = generate_state()
        assert s1 != s2
        assert len(s1) > 20

    def test_generate_code_is_unique(self):
        c1 = generate_code()
        c2 = generate_code()
        assert c1 != c2
        assert len(c1) > 20


class TestStoreAuthState:
    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_store_auth_state(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        state = AuthState(
            provider_name="oidc",
            client_redirect_uri="http://localhost:10000/login",
            client_state="client-state-123",
            code_challenge="challenge-abc",
            code_challenge_method="S256",
            idp_state="idp-state-xyz",
            nonce="nonce-456",
            credential_type="api_token",
        )

        result = await store_auth_state(state)

        assert result == "idp-state-xyz"
        redis.set.assert_called_once()
        call_args = redis.set.call_args
        assert call_args[0][0] == AUTH_STATE_PREFIX + "idp-state-xyz"
        assert call_args[1]["ex"] == AUTH_STATE_TTL

        # Verify the stored JSON contains all fields
        stored_data = json.loads(call_args[0][1])
        assert stored_data["provider_name"] == "oidc"
        assert stored_data["credential_type"] == "api_token"
        assert stored_data["nonce"] == "nonce-456"


class TestConsumeAuthState:
    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_consume_existing_state(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        pipe = AsyncMock()
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=False)
        redis.pipeline = MagicMock(return_value=pipe)

        state_data = {
            "provider_name": "oidc",
            "client_redirect_uri": "http://localhost:10000/login",
            "client_state": "client-state-123",
            "code_challenge": "challenge-abc",
            "code_challenge_method": "S256",
            "idp_state": "idp-state-xyz",
            "nonce": None,
            "credential_type": "session",
        }
        pipe.execute.return_value = [json.dumps(state_data), 1]

        result = await consume_auth_state("idp-state-xyz")

        assert result is not None
        assert result.provider_name == "oidc"
        assert result.client_redirect_uri == "http://localhost:10000/login"
        assert result.code_challenge == "challenge-abc"
        assert result.credential_type == "session"

        # Verify atomic get+delete
        pipe.get.assert_called_once_with(AUTH_STATE_PREFIX + "idp-state-xyz")
        pipe.delete.assert_called_once_with(AUTH_STATE_PREFIX + "idp-state-xyz")

    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_consume_expired_state_returns_none(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        pipe = AsyncMock()
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=False)
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.execute.return_value = [None, 0]

        result = await consume_auth_state("nonexistent")
        assert result is None


class TestStoreAuthCode:
    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_store_auth_code(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        auth_code = AuthCode(
            email="test@example.com",
            roles=["admin"],
            provider_name="local",
            code_challenge="challenge-abc",
            code_challenge_method="S256",
            display_name="Test User",
            credential_type="api_token",
        )

        await store_auth_code("code-123", auth_code)

        redis.set.assert_called_once()
        call_args = redis.set.call_args
        assert call_args[0][0] == AUTH_CODE_PREFIX + "code-123"
        assert call_args[1]["ex"] == AUTH_CODE_TTL

        stored_data = json.loads(call_args[0][1])
        assert stored_data["email"] == "test@example.com"
        assert stored_data["credential_type"] == "api_token"


class TestConsumeAuthCode:
    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_consume_existing_code(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        pipe = AsyncMock()
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=False)
        redis.pipeline = MagicMock(return_value=pipe)

        code_data = {
            "email": "test@example.com",
            "roles": ["admin", "audit"],
            "provider_name": "local",
            "code_challenge": "challenge-abc",
            "code_challenge_method": "S256",
            "display_name": "Test",
            "max_session_ttl": 3600,
            "credential_type": "session",
        }
        pipe.execute.return_value = [json.dumps(code_data), 1]

        result = await consume_auth_code("code-123")

        assert result is not None
        assert result.email == "test@example.com"
        assert result.roles == ["admin", "audit"]
        assert result.max_session_ttl == 3600
        assert result.credential_type == "session"

        pipe.get.assert_called_once_with(AUTH_CODE_PREFIX + "code-123")
        pipe.delete.assert_called_once_with(AUTH_CODE_PREFIX + "code-123")

    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_consume_expired_code_returns_none(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        pipe = AsyncMock()
        pipe.__aenter__ = AsyncMock(return_value=pipe)
        pipe.__aexit__ = AsyncMock(return_value=False)
        redis.pipeline = MagicMock(return_value=pipe)
        pipe.execute.return_value = [None, 0]

        result = await consume_auth_code("expired-code")
        assert result is None
