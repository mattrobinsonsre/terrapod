"""A Vault read returns its lease beside the data (#1648).

A template can read ``_lease.ttl``/``renewable``/``expires_at``, and a later
change revokes by ``lease_id`` (#1649), so the read keeps all of them in a
typed result. Driven through ``httpx.MockTransport`` so the real response
parsing runs. The lease id and the data are kept out of ``repr``.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from terrapod.config import VaultInstanceConfig
from terrapod.services import vault_client
from terrapod.services.vault_client import (
    VaultLease,
    VaultResponse,
    read_secret_data,
    read_secret_response,
    reset_token_cache,
)

LEASE_ID = "database/creds/ro/LEASE-ID-MUST-NOT-LEAK"


def _inst() -> VaultInstanceConfig:
    return VaultInstanceConfig(
        name="default",
        address="https://vault.test:8200",
        auth={"method": "token", "mount": "token", "role": "n/a"},
    )


def _serving(body: dict):
    real = httpx.AsyncClient

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    reset_token_cache()
    return patch.object(
        vault_client.httpx,
        "AsyncClient",
        lambda *a, **kw: real(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_a_dynamic_read_carries_its_lease():
    body = {
        "lease_id": LEASE_ID,
        "lease_duration": 1800,
        "renewable": True,
        "data": {"username": "u", "password": "p"},
    }
    before = datetime.now(UTC)
    with _serving(body):
        resp = await read_secret_response(
            _inst(), mount="database", path="creds/ro", engine="dynamic", static_token="t"
        )
    assert isinstance(resp, VaultResponse)
    assert resp.data == {"username": "u", "password": "p"}
    assert resp.lease is not None
    assert resp.lease.duration == 1800
    assert resp.lease.renewable is True
    assert resp.lease.lease_id == LEASE_ID
    assert before <= resp.lease.received_at <= datetime.now(UTC)
    assert resp.lease.expires_at == resp.lease.received_at + timedelta(seconds=1800)


@pytest.mark.asyncio
async def test_a_kv2_read_has_no_lease():
    body = {"lease_id": "", "lease_duration": 0, "renewable": False, "data": {"data": {"k": "v"}}}
    with _serving(body):
        resp = await read_secret_response(_inst(), mount="kvv2", path="a", static_token="t")
    assert resp.data == {"k": "v"}
    assert resp.lease is None


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["soon", None, -5])
async def test_a_malformed_or_missing_duration_is_no_lease_not_a_failure(duration):
    body = {"lease_duration": duration, "data": {"k": "v"}}
    with _serving(body):
        resp = await read_secret_response(
            _inst(), mount="x", path="y", engine="dynamic", static_token="t"
        )
    assert resp.data == {"k": "v"}
    assert resp.lease is None


@pytest.mark.asyncio
async def test_read_secret_data_still_returns_just_the_map():
    body = {"lease_id": LEASE_ID, "lease_duration": 60, "data": {"k": "v"}}
    with _serving(body):
        got = await read_secret_data(
            _inst(), mount="x", path="y", engine="dynamic", static_token="t"
        )
    assert got == {"k": "v"}


def test_template_metadata_exposes_no_lease_id_and_formats_the_expiry_rfc3339():
    lease = VaultLease(
        duration=90,
        renewable=False,
        received_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        lease_id=LEASE_ID,
    )
    assert lease.template_metadata() == {
        "ttl": 90,
        "renewable": False,
        "expires_at": "2026-01-02T03:05:35Z",
    }
    assert LEASE_ID not in repr(lease)
    assert LEASE_ID not in repr(VaultResponse(data={"password": "p"}, lease=lease))
    assert "'p'" not in repr(VaultResponse(data={"password": "p"}))
