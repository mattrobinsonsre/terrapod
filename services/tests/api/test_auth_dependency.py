"""Tests for the unified auth dependency (session + API token + listener cert)."""

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from terrapod.api.dependencies import (
    AuthenticatedUser,
    authenticate_listener,
    get_current_user,
    get_listener_identity,
    require_admin,
    require_admin_or_audit,
)
from terrapod.auth.ca import (
    CertificateAuthority,
    get_certificate_fingerprint,
    serialize_certificate,
)


def _mock_request(client_host: str = "127.0.0.1", headers: dict | None = None):
    """Create a mock Request with client IP and headers."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = client_host
    request.headers = headers or {}
    return request


class TestGetCurrentUser:
    @patch("terrapod.api.dependencies._resolve_user_roles", return_value=["everyone"])
    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_api_token_takes_priority(
        self, mock_validate_token, mock_get_session, mock_resolve_roles
    ):
        """If token matches an API token, session is not checked."""
        mock_token = MagicMock()
        mock_token.user_email = "bot@example.com"
        mock_validate_token.return_value = mock_token

        request = _mock_request()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test.tpod.token")
        mock_db = AsyncMock()

        user = await get_current_user(request=request, credentials=credentials, db=mock_db)

        assert user.email == "bot@example.com"
        assert user.auth_method == "api_token"
        assert user.roles == ["everyone"]
        mock_get_session.assert_not_called()

    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_falls_back_to_session(self, mock_validate_token, mock_get_session):
        """If token is not an API token, check Redis sessions."""
        mock_validate_token.return_value = None

        mock_session = MagicMock()
        mock_session.email = "user@example.com"
        mock_session.display_name = "User"
        mock_session.roles = ["admin"]
        mock_session.provider_name = "local"
        mock_session.last_active_at = "2026-01-01T00:00:00+00:00"
        mock_get_session.return_value = mock_session

        # Mock _should_refresh_session to return False
        with patch("terrapod.api.dependencies._should_refresh_session", return_value=False):
            request = _mock_request()
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")
            mock_db = AsyncMock()

            user = await get_current_user(request=request, credentials=credentials, db=mock_db)

        assert user.email == "user@example.com"
        assert user.auth_method == "session"
        assert user.roles == ["admin"]

    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_neither_match_raises_401(self, mock_validate_token, mock_get_session):
        """If neither API token nor session matches, raise 401."""
        mock_validate_token.return_value = None
        mock_get_session.return_value = None

        request = _mock_request()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid-token")
        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request=request, credentials=credentials, db=mock_db)

        assert exc_info.value.status_code == 401

    @patch("terrapod.api.dependencies.refresh_session")
    @patch("terrapod.api.dependencies._should_refresh_session", return_value=True)
    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_session_refresh_on_stale(
        self,
        mock_validate_token,
        mock_get_session,
        mock_should_refresh,
        mock_refresh,
    ):
        """Stale sessions trigger a TTL refresh."""
        mock_validate_token.return_value = None

        mock_session = MagicMock()
        mock_session.email = "user@example.com"
        mock_session.display_name = None
        mock_session.roles = []
        mock_session.provider_name = "oidc"
        mock_get_session.return_value = mock_session

        request = _mock_request()
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="stale-session")
        mock_db = AsyncMock()

        await get_current_user(request=request, credentials=credentials, db=mock_db)

        mock_refresh.assert_called_once_with("stale-session", mock_session)

    async def test_no_credentials_raises_401(self):
        """No Bearer token → 401."""
        request = _mock_request()
        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request=request, credentials=None, db=mock_db)

        assert exc_info.value.status_code == 401


class TestRequireAdmin:
    async def test_admin_passes(self):
        user = AuthenticatedUser(
            email="admin@example.com",
            display_name="Admin",
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )
        result = await require_admin(user=user)
        assert result.email == "admin@example.com"

    async def test_non_admin_raises_403(self):
        user = AuthenticatedUser(
            email="user@example.com",
            display_name="User",
            roles=["viewer"],
            provider_name="local",
            auth_method="session",
        )
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=user)

        assert exc_info.value.status_code == 403


class TestRequireAdminOrAudit:
    async def test_admin_passes(self):
        user = AuthenticatedUser(
            email="admin@example.com",
            display_name=None,
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )
        result = await require_admin_or_audit(user=user)
        assert result is user

    async def test_audit_passes(self):
        user = AuthenticatedUser(
            email="auditor@example.com",
            display_name=None,
            roles=["audit"],
            provider_name="local",
            auth_method="session",
        )
        result = await require_admin_or_audit(user=user)
        assert result is user

    async def test_neither_raises_403(self):
        user = AuthenticatedUser(
            email="user@example.com",
            display_name=None,
            roles=["viewer", "dev"],
            provider_name="local",
            auth_method="session",
        )
        with pytest.raises(HTTPException) as exc_info:
            await require_admin_or_audit(user=user)

        assert exc_info.value.status_code == 403


# ── Listener certificate auth ──────────────────────────────────────────


@pytest.fixture(scope="module")
def _test_ca() -> CertificateAuthority:
    """Module-scoped CA so we don't pay 30 cert-generations per test."""
    return CertificateAuthority.generate()


def _cert_header(cert) -> str:
    """Encode a cert as the X-Terrapod-Client-Cert header value."""
    return base64.b64encode(serialize_certificate(cert)).decode()


class TestGetListenerIdentity:
    """Cert auth must accept any fingerprint registered in Redis, not just one.

    Concurrent /renew calls register multiple valid fingerprints. Auth treats
    each as independently valid until the per-fingerprint key TTLs out. This
    is the regression test for the scenario where pod A and pod B both renew
    in the same window and one of their certs ends up unused — but both must
    still authenticate, otherwise the listener fleet 401-loops itself.
    """

    @pytest.mark.asyncio
    async def test_concurrent_renewals_both_authenticate(self, _test_ca):
        """Two certs issued seconds apart must both pass cert-auth."""
        listener_name = "listener-1"
        listener_id = str(uuid.uuid4())
        pool_id = str(uuid.uuid4())

        cert_a, _ = _test_ca.issue_listener_certificate(listener_name, "pool-1")
        cert_b, _ = _test_ca.issue_listener_certificate(listener_name, "pool-1")
        fp_a = get_certificate_fingerprint(cert_a)
        fp_b = get_certificate_fingerprint(cert_b)
        assert fp_a != fp_b  # different keys → different fingerprints

        # Redis state: both fingerprints registered (the bug fix). The hash
        # carries the latest fingerprint for UI display only — auth ignores it.
        listener_dict = {
            "id": listener_id,
            "name": listener_name,
            "pool_id": pool_id,
            "certificate_fingerprint": fp_b,  # whichever got hset last
        }

        async def fake_is_valid(_lid, fp, listener=None):
            return fp in (fp_a, fp_b)

        with (
            patch("terrapod.auth.ca.get_ca", return_value=_test_ca),
            patch(
                "terrapod.services.agent_pool_service.get_listener_by_name",
                AsyncMock(return_value=listener_dict),
            ),
            patch(
                "terrapod.services.agent_pool_service.is_fingerprint_valid",
                side_effect=fake_is_valid,
            ),
        ):
            id_a = await get_listener_identity(_cert_header(cert_a))
            id_b = await get_listener_identity(_cert_header(cert_b))

        assert str(id_a.listener_id) == listener_id
        assert str(id_b.listener_id) == listener_id
        assert id_a.certificate_fingerprint == fp_a
        assert id_b.certificate_fingerprint == fp_b

    @pytest.mark.asyncio
    async def test_unregistered_fingerprint_rejected(self, _test_ca):
        """A CA-signed cert whose fingerprint is not registered → 401.

        Defends against a stale cert from a prior listener instance whose
        Redis registration has aged out, or a forged cert (signed by another
        instance of the same CA) that was never issued by this server.
        """
        listener_name = "listener-1"
        cert, _ = _test_ca.issue_listener_certificate(listener_name, "pool-1")

        with (
            patch("terrapod.auth.ca.get_ca", return_value=_test_ca),
            patch(
                "terrapod.services.agent_pool_service.get_listener_by_name",
                AsyncMock(
                    return_value={
                        "id": str(uuid.uuid4()),
                        "name": listener_name,
                        "pool_id": str(uuid.uuid4()),
                    }
                ),
            ),
            patch(
                "terrapod.services.agent_pool_service.is_fingerprint_valid",
                AsyncMock(return_value=False),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_listener_identity(_cert_header(cert))

        assert exc_info.value.status_code == 401
        assert "not registered" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_listener_not_in_redis_rejected(self, _test_ca):
        """Cert authenticates against CA but listener has no Redis registration."""
        cert, _ = _test_ca.issue_listener_certificate("ghost", "pool-1")

        with (
            patch("terrapod.auth.ca.get_ca", return_value=_test_ca),
            patch(
                "terrapod.services.agent_pool_service.get_listener_by_name",
                AsyncMock(return_value=None),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_listener_identity(_cert_header(cert))

        assert exc_info.value.status_code == 401


class TestAuthenticateListener:
    """Same coverage as get_listener_identity but for the SSE-path variant.

    `authenticate_listener` is the Request-based form used by SSE handlers
    (it must not hold a DB session for the streaming lifetime). Same cert
    handling, same fingerprint check — keep them in lockstep.
    """

    @pytest.mark.asyncio
    async def test_concurrent_renewals_both_authenticate(self, _test_ca):
        listener_name = "listener-1"
        listener_id = str(uuid.uuid4())
        cert_a, _ = _test_ca.issue_listener_certificate(listener_name, "pool-1")
        cert_b, _ = _test_ca.issue_listener_certificate(listener_name, "pool-1")
        fp_a = get_certificate_fingerprint(cert_a)
        fp_b = get_certificate_fingerprint(cert_b)

        listener_dict = {
            "id": listener_id,
            "name": listener_name,
            "pool_id": str(uuid.uuid4()),
            "certificate_fingerprint": fp_b,
        }

        async def fake_is_valid(_lid, fp, listener=None):
            return fp in (fp_a, fp_b)

        request_a = MagicMock()
        request_a.headers = {"x-terrapod-client-cert": _cert_header(cert_a)}
        request_b = MagicMock()
        request_b.headers = {"x-terrapod-client-cert": _cert_header(cert_b)}

        with (
            patch("terrapod.auth.ca.get_ca", return_value=_test_ca),
            patch(
                "terrapod.services.agent_pool_service.get_listener_by_name",
                AsyncMock(return_value=listener_dict),
            ),
            patch(
                "terrapod.services.agent_pool_service.is_fingerprint_valid",
                side_effect=fake_is_valid,
            ),
        ):
            id_a = await authenticate_listener(request_a)
            id_b = await authenticate_listener(request_b)

        assert id_a.certificate_fingerprint == fp_a
        assert id_b.certificate_fingerprint == fp_b

    @pytest.mark.asyncio
    async def test_unregistered_fingerprint_rejected(self, _test_ca):
        cert, _ = _test_ca.issue_listener_certificate("listener-1", "pool-1")
        request = MagicMock()
        request.headers = {"x-terrapod-client-cert": _cert_header(cert)}

        with (
            patch("terrapod.auth.ca.get_ca", return_value=_test_ca),
            patch(
                "terrapod.services.agent_pool_service.get_listener_by_name",
                AsyncMock(
                    return_value={
                        "id": str(uuid.uuid4()),
                        "name": "listener-1",
                        "pool_id": str(uuid.uuid4()),
                    }
                ),
            ),
            patch(
                "terrapod.services.agent_pool_service.is_fingerprint_valid",
                AsyncMock(return_value=False),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await authenticate_listener(request)

        assert exc_info.value.status_code == 401
        assert "not registered" in exc_info.value.detail.lower()
