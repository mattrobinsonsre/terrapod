"""Peer credentials are accepted in exactly one place (#960 phase 3, #1110).

A peer may read entities an ordinary user could not — resolved sensitive
variables among them — so the containment is the security property, not the
authentication. A `peer` token resolves to no roles, which already fails every
RBAC check; the gap these tests close is the handful of endpoints that require
only "some authenticated principal" and would otherwise be satisfied by one.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import (
    PEER_KIND,
    authenticate_request,
    get_current_user,
    get_peer_identity,
)


def _token(kind=PEER_KIND, created_by="oauth-client:peer-b"):
    tok = MagicMock()
    tok.kind = kind
    tok.bound_to = None
    tok.created_by = created_by
    tok.id = "tok-1"
    tok.pinned_roles = None
    return tok


def _request(auth="Bearer peer-token"):
    req = MagicMock()
    req.headers = {"authorization": auth} if auth else {}
    req.state = MagicMock()
    return req


class TestPeerIsNotAUser:
    """The containment. Without it, a peer could create a workspace."""

    @patch("terrapod.api.dependencies.validate_api_token", new_callable=AsyncMock)
    async def test_get_current_user_refuses_a_peer_token(self, mock_validate):
        mock_validate.return_value = _token()
        creds = MagicMock()
        creds.credentials = "peer-token"

        with pytest.raises(HTTPException) as exc:
            await get_current_user(_request(), creds, AsyncMock())

        assert exc.value.status_code == 401

    @patch("terrapod.api.dependencies.validate_api_token", new_callable=AsyncMock)
    async def test_sse_auth_refuses_a_peer_token(self, mock_validate):
        """The SSE path resolves auth separately and needs the same guard."""
        mock_validate.return_value = _token()

        with (
            patch("terrapod.db.session.get_db_session") as mock_session,
            pytest.raises(HTTPException) as exc,
        ):
            mock_session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            await authenticate_request(_request())

        assert exc.value.status_code == 401

    @patch("terrapod.api.dependencies.validate_api_token", new_callable=AsyncMock)
    @patch("terrapod.api.dependencies._resolve_user_roles", new_callable=AsyncMock)
    async def test_an_ordinary_token_still_works(self, mock_roles, mock_validate):
        """The far more dangerous direction — this guard must not lock users out."""
        mock_roles.return_value = ["everyone"]
        tok = _token(kind="interactive")
        tok.bound_to = "a@example.com"
        mock_validate.return_value = tok
        creds = MagicMock()
        creds.credentials = "user-token"

        user = await get_current_user(_request(), creds, AsyncMock())

        assert user.email == "a@example.com"


class TestGetPeerIdentity:
    @patch("terrapod.api.dependencies.validate_api_token", new_callable=AsyncMock)
    async def test_accepts_a_peer_token(self, mock_validate):
        mock_validate.return_value = _token()

        peer = await get_peer_identity(_request(), AsyncMock())

        assert peer.client_id == "peer-b"

    @patch("terrapod.api.dependencies.validate_api_token", new_callable=AsyncMock)
    async def test_rejects_an_ordinary_user_token(self, mock_validate):
        """A user must not be able to read the replication surface by holding a
        perfectly valid token — that is how a peer's wider visibility leaks."""
        mock_validate.return_value = _token(kind="interactive")

        with pytest.raises(HTTPException) as exc:
            await get_peer_identity(_request(), AsyncMock())

        assert exc.value.status_code == 401

    @patch("terrapod.api.dependencies.validate_api_token", new_callable=AsyncMock)
    async def test_rejects_an_unknown_token(self, mock_validate):
        mock_validate.return_value = None

        with pytest.raises(HTTPException) as exc:
            await get_peer_identity(_request(), AsyncMock())

        assert exc.value.status_code == 401

    @patch("terrapod.api.dependencies.validate_api_token", new_callable=AsyncMock)
    async def test_failures_are_indistinguishable(self, mock_validate):
        """Unknown and valid-but-not-a-peer must look identical, or the endpoint
        becomes an oracle for which tokens exist."""
        details = []
        for token in (None, _token(kind="interactive"), _token(kind="service_detached")):
            mock_validate.return_value = token
            with pytest.raises(HTTPException) as exc:
                await get_peer_identity(_request(), AsyncMock())
            details.append((exc.value.status_code, exc.value.detail))

        assert len(set(details)) == 1, f"failure modes are distinguishable: {details}"

    async def test_requires_a_bearer_header(self):
        with pytest.raises(HTTPException) as exc:
            await get_peer_identity(_request(auth=""), AsyncMock())

        assert exc.value.status_code == 401
