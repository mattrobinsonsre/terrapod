"""The client_credentials grant (#1108, phase 2 of #960).

Introduced for the HA peer link: the two nodes authenticate with a standard
grant rather than a bespoke handshake, so a reviewer can read the RFC and know
what it guarantees.

**The expected credential is config, not a stored row** (#1171). Both halves are
declared in the chart, so a persisted copy could only ever be a second — and
disagreeing — statement of the same fact.

Pinned here are the properties that survive that change: failures are
indistinguishable so nothing can be enumerated, an unpaired node accepts
nothing, and the authorization_code flow is untouched. The DB-touching half
(what identity the issued token carries, and rotation) is in
`tests/integration/test_peer_credentials.py`, against a real database.
"""

import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.routers.oauth import _client_credentials_grant, _hash_client_secret
from terrapod.config import HAConfig, HAPeerConfig, HAPeerInboundConfig

pytestmark = pytest.mark.asyncio


def _ha(client_id: str = "peer-b", client_secret: str = "s3cret") -> HAConfig:
    """The real config object rather than a stub — the grant reads it directly,
    so a SimpleNamespace would only prove the patch worked."""
    return HAConfig(
        peer=HAPeerConfig(
            inbound=HAPeerInboundConfig(client_id=client_id, client_secret=client_secret)
        )
    )


async def _grant(ha: HAConfig, client_id: str, client_secret: str):
    with patch("terrapod.api.routers.oauth.settings.ha", ha):
        return await _client_credentials_grant(AsyncMock(), client_id, client_secret)


class TestHashing:
    @staticmethod
    async def test_matches_the_api_token_scheme():
        assert _hash_client_secret("abc") == hashlib.sha256(b"abc").hexdigest()


class TestFailuresAreIndistinguishable:
    """A caller must not be able to work out which half was wrong, or whether
    the node is paired at all."""

    async def _refusal(self, ha: HAConfig, client_id: str, secret: str) -> HTTPException:
        with pytest.raises(HTTPException) as exc:
            await _grant(ha, client_id, secret)
        return exc.value

    async def test_unknown_client_id(self):
        assert (await self._refusal(_ha(), "someone-else", "s3cret")).status_code == 401

    async def test_wrong_secret(self):
        assert (await self._refusal(_ha(), "peer-b", "wrong")).status_code == 401

    async def test_an_unpaired_node(self):
        assert (await self._refusal(_ha("", ""), "peer-b", "s3cret")).status_code == 401

    async def test_all_three_are_identical(self):
        refusals = [
            await self._refusal(_ha(), "someone-else", "s3cret"),
            await self._refusal(_ha(), "peer-b", "wrong"),
            await self._refusal(_ha("", ""), "peer-b", "s3cret"),
        ]

        assert {(r.status_code, r.detail) for r in refusals} == {
            (401, "Invalid client credentials")
        }


class TestUnpairedNode:
    async def test_empty_credentials_do_not_match_an_empty_config(self):
        """The degenerate case a config comparison creates and must close: with
        nothing configured both sides are "", and `compare_digest` would call
        that a match."""
        with pytest.raises(HTTPException) as exc:
            await _grant(_ha("", ""), "", "")

        assert exc.value.status_code in (400, 401)

    async def test_a_client_id_without_a_secret_accepts_nothing(self):
        # Half-configured is not configured: naming the credential in a values
        # file must not make the node accept an empty secret for it.
        with pytest.raises(HTTPException):
            await _grant(_ha("peer-b", ""), "peer-b", "")
        with pytest.raises(HTTPException):
            await _grant(_ha("peer-b", ""), "peer-b", "guess")


class TestMissingCredentials:
    async def test_no_client_id(self):
        with pytest.raises(HTTPException) as exc:
            await _grant(_ha(), "", "s3cret")
        assert exc.value.status_code == 400

    async def test_no_secret(self):
        with pytest.raises(HTTPException) as exc:
            await _grant(_ha(), "peer-b", "")
        assert exc.value.status_code == 400

    @patch("terrapod.api.routers.oauth.create_api_token")
    async def test_never_issues_a_token_without_credentials(self, mock_create):
        with pytest.raises(HTTPException):
            await _grant(_ha(), "", "")

        assert not mock_create.called
