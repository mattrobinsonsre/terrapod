"""Tests for Redis-backed ephemeral auth state."""

import json
from unittest.mock import AsyncMock, patch

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
            idp_code_verifier="upstream-verifier",
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
        assert stored_data["idp_code_verifier"] == "upstream-verifier"


class TestConsumeAuthState:
    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_consume_existing_state(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        state_data = {
            "provider_name": "oidc",
            "client_redirect_uri": "http://localhost:10000/login",
            "client_state": "client-state-123",
            "code_challenge": "challenge-abc",
            "code_challenge_method": "S256",
            "idp_state": "idp-state-xyz",
            "nonce": None,
            "idp_code_verifier": "upstream-verifier",
            "credential_type": "session",
        }
        redis.getdel = AsyncMock(return_value=json.dumps(state_data))

        result = await consume_auth_state("idp-state-xyz")

        assert result is not None
        assert result.provider_name == "oidc"
        assert result.client_redirect_uri == "http://localhost:10000/login"
        assert result.code_challenge == "challenge-abc"
        assert result.idp_code_verifier == "upstream-verifier"
        assert result.credential_type == "session"

        # Verify atomic get+delete
        # One atomic GETDEL, not a GET then a DELETE: a pipeline without
        # MULTI/EXEC lets two clients redeem the same one-time value.
        redis.getdel.assert_awaited_once_with(AUTH_STATE_PREFIX + "idp-state-xyz")

    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_consume_expired_state_returns_none(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        redis.getdel = AsyncMock(return_value=None)

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
        redis.getdel = AsyncMock(return_value=json.dumps(code_data))

        result = await consume_auth_code("code-123")

        assert result is not None
        assert result.email == "test@example.com"
        assert result.roles == ["admin", "audit"]
        assert result.max_session_ttl == 3600
        assert result.credential_type == "session"

        # One atomic GETDEL, not a GET then a DELETE: a pipeline without
        # MULTI/EXEC lets two clients redeem the same one-time value.
        redis.getdel.assert_awaited_once_with(AUTH_CODE_PREFIX + "code-123")

    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_consume_expired_code_returns_none(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        redis.getdel = AsyncMock(return_value=None)

        result = await consume_auth_code("expired-code")
        assert result is None


class TestRollingUpgradeTolerance:
    """A state written by a newer replica must not break an older one.

    Auth state is JSON in a shared Redis, read by whichever API pod the browser
    lands on. During a rolling upgrade the two builds disagree about the fields,
    so a newer pod writes one the older pod has never heard of — and
    `AuthState(**parsed)` would raise `TypeError: unexpected keyword argument`.
    The user sees a login fail with nothing to explain it, on a pod that is
    behaving perfectly.

    `saml_request_id` is the field that made this concrete; the tolerance is
    written once so the next one costs nothing.
    """

    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_a_field_this_build_does_not_know_is_ignored(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.getdel = AsyncMock(
            return_value=json.dumps(
                {
                    "provider_name": "okta",
                    "client_redirect_uri": "https://terrapod.example.com/auth/callback",
                    "client_state": "cs",
                    "code_challenge": "cc",
                    "code_challenge_method": "S256",
                    "idp_state": "idp-state-xyz",
                    "a_field_from_a_future_release": "whatever",
                }
            )
        )

        result = await consume_auth_state("idp-state-xyz")

        assert result is not None
        assert result.provider_name == "okta"
        assert not hasattr(result, "a_field_from_a_future_release")

    @patch("terrapod.auth.auth_state.get_redis_client")
    async def test_an_older_state_without_the_saml_request_id_still_loads(self, mock_get_redis):
        """The other direction: a state written before this change.

        It loads with `saml_request_id` unset, and the SAML ACS endpoint then
        refuses the login rather than skipping the InResponseTo check — which is
        the right way round for a window that lasts one deploy.
        """
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.getdel = AsyncMock(
            return_value=json.dumps(
                {
                    "provider_name": "azure-ad",
                    "client_redirect_uri": "https://terrapod.example.com/auth/callback",
                    "client_state": "cs",
                    "code_challenge": "cc",
                    "code_challenge_method": "S256",
                    "idp_state": "idp-state-xyz",
                }
            )
        )

        result = await consume_auth_state("idp-state-xyz")

        assert result is not None
        assert result.saml_request_id is None
