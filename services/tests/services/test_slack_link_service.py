"""Tests for the Slack account-linking signed state (#556)."""

import time
from unittest.mock import patch

import pytest

from terrapod.services import slack_link_service as svc


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, k, v, ex=None, nx=None):
        self.store[k] = v
        return True

    async def getdel(self, k):
        return self.store.pop(k, None)


@pytest.mark.asyncio
async def test_state_roundtrip_and_single_use():
    fake = FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=fake):
        state = await svc.mint_link_state("T123", "U456", "https://hooks.slack/resp")
        team, user, response_url = await svc.verify_and_consume_state(state)
        assert (team, user) == ("T123", "U456")
        assert response_url == "https://hooks.slack/resp"
        # Nonce is burned — a replay must fail.
        with pytest.raises(svc.LinkStateError):
            await svc.verify_and_consume_state(state)


@pytest.mark.asyncio
async def test_tampered_signature_rejected():
    fake = FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=fake):
        state = await svc.mint_link_state("T1", "U1")
        payload_b64, _sig = state.split(".", 1)
        forged = f"{payload_b64}.{svc._b64u(b'not-the-real-hmac-signature-here!!')}"
        with pytest.raises(svc.LinkStateError):
            await svc.verify_and_consume_state(forged)


@pytest.mark.asyncio
async def test_expired_state_rejected():
    fake = FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=fake):
        state = await svc.mint_link_state("T1", "U1")
        # Advance time past the TTL so the exp check trips.
        with patch.object(time, "time", return_value=time.time() + svc._STATE_TTL_SECONDS + 10):
            with pytest.raises(svc.LinkStateError):
                await svc.verify_and_consume_state(state)


@pytest.mark.asyncio
async def test_malformed_state_rejected():
    with pytest.raises(svc.LinkStateError):
        await svc.verify_and_consume_state("not-a-valid-token")
