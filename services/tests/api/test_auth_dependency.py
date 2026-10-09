"""Tests for the unified auth dependency (session + API token + listener cert)."""

import base64
import uuid
from contextlib import contextmanager
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
        mock_token.bound_to = "bot@example.com"
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


class _StubRequest:
    """Minimal stand-in for the parts of Request the listener auth path reads."""

    def __init__(self, method: str = "GET", path: str = "/api/v1/x", headers: dict | None = None):
        self.method = method
        self.headers = headers or {}
        self.url = type("U", (), {"path": path})()


@contextmanager
def _pop(enabled: bool):
    """Turn listener proof-of-possession on or off for one block.

    Explicit rather than an autouse fixture: a file-wide switch that disables a
    security control would silently cover every test added here later, including
    ones that ought to be asserting it.
    """
    from terrapod.config import settings

    cfg = settings.agent_pools
    old = cfg.require_listener_proof_of_possession
    cfg.require_listener_proof_of_possession = enabled
    try:
        yield
    finally:
        cfg.require_listener_proof_of_possession = old


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
            _pop(False),
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
            id_a = await get_listener_identity(_StubRequest(), _cert_header(cert_a))
            id_b = await get_listener_identity(_StubRequest(), _cert_header(cert_b))

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
            _pop(False),
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
                await get_listener_identity(_StubRequest(), _cert_header(cert))

        assert exc_info.value.status_code == 401
        assert "not registered" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_listener_not_in_redis_rejected(self, _test_ca):
        """Cert authenticates against CA but listener has no Redis registration."""
        cert, _ = _test_ca.issue_listener_certificate("ghost", "pool-1")

        with (
            _pop(False),
            patch("terrapod.auth.ca.get_ca", return_value=_test_ca),
            patch(
                "terrapod.services.agent_pool_service.get_listener_by_name",
                AsyncMock(return_value=None),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_listener_identity(_StubRequest(), _cert_header(cert))

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
            _pop(False),
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
            _pop(False),
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


class TestListenerProofOfPossessionIsEnforcedOnBothPaths:
    """With the setting on, a certificate on its own must not authenticate.

    Both paths are covered deliberately. `authenticate_listener` exists because
    SSE endpoints cannot hold a yield-dependency, so it is a second, separate
    implementation of listener auth — and a replay hole in it would be just as
    complete as one in the dependency, while being easy to miss.
    """

    @staticmethod
    def _patches(listener_dict, _test_ca):
        return (
            patch("terrapod.auth.ca.get_ca", return_value=_test_ca),
            patch(
                "terrapod.services.agent_pool_service.get_listener_by_name",
                AsyncMock(return_value=listener_dict),
            ),
            patch(
                "terrapod.services.agent_pool_service.is_fingerprint_valid",
                AsyncMock(return_value=True),
            ),
        )

    @staticmethod
    def _listener():
        return {"id": str(uuid.uuid4()), "name": "listener-1", "pool_id": str(uuid.uuid4())}

    def _signed_headers(self, key_pem, cert, method, path):
        import secrets
        import time

        from terrapod.auth.listener_pop import (
            NONCE_HEADER,
            SIGNATURE_HEADER,
            TIMESTAMP_HEADER,
            sign_request,
        )

        ts, nonce = str(int(time.time())), secrets.token_urlsafe(24)
        return {
            "x-terrapod-client-cert": _cert_header(cert),
            TIMESTAMP_HEADER.lower(): ts,
            NONCE_HEADER.lower(): nonce,
            SIGNATURE_HEADER.lower(): sign_request(key_pem, method, path, ts, nonce),
        }

    async def test_the_dependency_refuses_a_certificate_with_no_signature(self, _test_ca):
        cert, _key = _test_ca.issue_listener_certificate("listener-1", "pool-1")
        ld = self._listener()
        p1, p2, p3 = self._patches(ld, _test_ca)
        with _pop(True), p1, p2, p3:
            with pytest.raises(HTTPException) as exc:
                await get_listener_identity(_StubRequest(), _cert_header(cert))
        assert exc.value.status_code == 401
        assert "proof of possession" in exc.value.detail.lower()

    async def test_the_sse_path_refuses_a_certificate_with_no_signature(self, _test_ca):
        cert, _key = _test_ca.issue_listener_certificate("listener-1", "pool-1")
        ld = self._listener()
        req = _StubRequest(headers={"x-terrapod-client-cert": _cert_header(cert)})
        p1, p2, p3 = self._patches(ld, _test_ca)
        with _pop(True), p1, p2, p3:
            with pytest.raises(HTTPException) as exc:
                await authenticate_listener(req)
        assert exc.value.status_code == 401
        assert "proof of possession" in exc.value.detail.lower()

    async def test_a_signed_request_is_accepted_on_the_sse_path(self, monkeypatch, _test_ca):
        cert, key = _test_ca.issue_listener_certificate("listener-1", "pool-1")
        from cryptography.hazmat.primitives import serialization

        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()

        class _R:
            def __init__(self):
                self.store: dict[str, str] = {}

            async def set(self, k, v, nx=False, ex=None):
                if nx and k in self.store:
                    return None
                self.store[k] = v
                return True

        monkeypatch.setattr("terrapod.redis.client.get_redis_client", lambda: _R())
        path = "/api/v1/listeners/listener-1/events"
        req = _StubRequest(path=path, headers=self._signed_headers(key_pem, cert, "GET", path))
        ld = self._listener()
        p1, p2, p3 = self._patches(ld, _test_ca)
        with _pop(True), p1, p2, p3:
            ident = await authenticate_listener(req)
        assert str(ident.listener_id) == ld["id"]


class TestTheRunnerTokenPhaseReachesThePrincipal:
    """The middle link of the phase chain, which nothing joined (#1901).

    Three things have to line up for a phase-conditioned cloud trust policy to
    work: the token must CARRY the phase, the principal must EXPOSE it, and the
    mint must USE it. The first and third were each tested in their own file —
    `tests/auth/test_runner_tokens.py` proves the token round-trips a phase, and
    `tests/api/test_cloud_identity.py` proves the handler reads
    `user.run_phase`. Both of those build their own principal, so nothing
    exercised the code that puts the phase on it.

    Deleting `run_phase=claims.phase` from both sites in `dependencies.py` left
    2,810 tests passing. Every minted token then loses its `phase` claim AND the
    `:phase:<phase>` suffix on `sub` — and for Azure a federated credential
    matches on issuer, subject and audience only, so `sub` is the one place a
    phase condition can be expressed there. Every phase-conditioned policy stops
    matching, silently, failing inside the cloud's token exchange with nothing
    wrong on Terrapod's side to look at.

    A real token through the real dependency, because that is the only thing
    that covers the assignment.
    """

    @pytest.mark.parametrize("phase", ["plan", "apply"])
    async def test_get_current_user_carries_the_phase_the_token_claims(self, phase):
        from terrapod.auth.runner_tokens import generate_runner_token

        run_id = str(uuid.uuid4())
        token = generate_runner_token(run_id, ttl=3600, phase=phase)

        user = await get_current_user(
            request=_mock_request(),
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            db=MagicMock(),
        )

        assert user.auth_method == "runner_token"
        assert user.run_id == run_id
        assert user.run_phase == phase, (
            "the phase claim did not reach the principal, so the mint cannot put "
            "it in the token it signs"
        )

    async def test_the_older_unphased_token_still_resolves_with_no_phase(self):
        """The negative path, and it is load-bearing rather than tidy: a lagging
        listener mints the five-field form, which must resolve as `None` —
        "no claim" — never as a mismatch or a refusal."""
        from terrapod.auth.runner_tokens import generate_runner_token

        run_id = str(uuid.uuid4())
        token = generate_runner_token(run_id, ttl=3600)

        user = await get_current_user(
            request=_mock_request(),
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            db=MagicMock(),
        )

        assert user.auth_method == "runner_token"
        assert user.run_phase is None

    @pytest.mark.parametrize("phase", ["plan", "apply"])
    async def test_authenticate_request_carries_it_too(self, phase):
        """The second site. `authenticate_request` is the SSE/streaming path —
        it manages its own short-lived DB session rather than taking `get_db` —
        so it populates the principal separately and can drift from the
        dependency above."""
        from terrapod.api.dependencies import authenticate_request
        from terrapod.auth.runner_tokens import generate_runner_token

        run_id = str(uuid.uuid4())
        token = generate_runner_token(run_id, ttl=3600, phase=phase)

        user = await authenticate_request(
            _mock_request(headers={"authorization": f"Bearer {token}"})
        )

        assert user is not None
        assert user.auth_method == "runner_token"
        assert user.run_phase == phase
