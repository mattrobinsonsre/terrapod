"""The client_credentials grant (#1108, phase 2 of #960).

Introduced for the HA peer link: each node registers a client representing its
peer and hands over those credentials, so the two authenticate with a standard
grant rather than a bespoke handshake.

The security-relevant properties are the ones worth pinning: failures are
indistinguishable from each other so client ids cannot be enumerated, the
issued identity is its own class rather than a reuse of the runner-token path,
and the existing authorization_code flow is untouched.
"""

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.routers.oauth import _client_credentials_grant, _hash_client_secret

pytestmark = pytest.mark.asyncio


def _client(secret="s3cret", active=True, client_id="peer-b", name="node-b"):
    c = MagicMock()
    c.client_id = client_id
    c.name = name
    c.is_active = active
    c.client_secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    return c


def _db(client):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = client
    db.execute.return_value = result
    return db


class TestHashing:
    @staticmethod
    async def test_matches_the_api_token_scheme():
        assert _hash_client_secret("abc") == hashlib.sha256(b"abc").hexdigest()


class TestSuccess:
    @patch("terrapod.api.routers.oauth.create_api_token")
    async def test_issues_a_peer_token(self, mock_create):
        mock_create.return_value = (MagicMock(id="tok-1"), "raw-token-value")
        db = _db(_client())

        resp = await _client_credentials_grant(db, "peer-b", "s3cret")

        assert resp.status_code == 200
        # The identity class is the point: a peer must not be minted as a
        # runner, a user, or a detached service token.
        assert mock_create.await_args.kwargs["kind"] == "peer"
        assert mock_create.await_args.kwargs["bound_to"] is None

    @patch("terrapod.api.routers.oauth.create_api_token")
    async def test_returns_a_bearer_token_with_expiry(self, mock_create):
        mock_create.return_value = (MagicMock(id="tok-1"), "raw-token-value")

        resp = await _client_credentials_grant(_db(_client()), "peer-b", "s3cret")

        body = bytes(resp.body).decode()
        assert "raw-token-value" in body
        assert '"token_type":"bearer"' in body.replace(" ", "")
        assert "expires_in" in body

    @patch("terrapod.api.routers.oauth.create_api_token")
    async def test_records_last_used(self, mock_create):
        mock_create.return_value = (MagicMock(id="tok-1"), "raw")
        client = _client()
        db = _db(client)

        await _client_credentials_grant(db, "peer-b", "s3cret")

        assert client.last_used_at is not None
        db.commit.assert_awaited()


class TestFailuresAreIndistinguishable:
    """Every failure returns the same 401 and the same message, so the endpoint
    cannot be used to discover which client ids exist."""

    async def test_unknown_client(self):
        with pytest.raises(HTTPException) as exc:
            await _client_credentials_grant(_db(None), "nope", "s3cret")
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid client credentials"

    async def test_wrong_secret(self):
        with pytest.raises(HTTPException) as exc:
            await _client_credentials_grant(_db(_client()), "peer-b", "wrong")
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid client credentials"

    async def test_deactivated_client(self):
        with pytest.raises(HTTPException) as exc:
            await _client_credentials_grant(_db(_client(active=False)), "peer-b", "s3cret")
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid client credentials"

    async def test_all_three_are_identical(self):
        details = []
        for db, cid, secret in (
            (_db(None), "nope", "s3cret"),
            (_db(_client()), "peer-b", "wrong"),
            (_db(_client(active=False)), "peer-b", "s3cret"),
        ):
            with pytest.raises(HTTPException) as exc:
                await _client_credentials_grant(db, cid, secret)
            details.append((exc.value.status_code, exc.value.detail))
        assert len(set(details)) == 1, f"failure modes are distinguishable: {details}"


class TestMissingCredentials:
    async def test_no_client_id(self):
        with pytest.raises(HTTPException) as exc:
            await _client_credentials_grant(_db(None), "", "s3cret")
        assert exc.value.status_code == 400

    async def test_no_secret(self):
        with pytest.raises(HTTPException) as exc:
            await _client_credentials_grant(_db(None), "peer-b", "")
        assert exc.value.status_code == 400

    async def test_never_issues_a_token_without_credentials(self):
        """A 400 must come before any lookup — no token, no DB round trip."""
        db = _db(None)
        with pytest.raises(HTTPException):
            await _client_credentials_grant(db, "", "")
        db.execute.assert_not_awaited()
