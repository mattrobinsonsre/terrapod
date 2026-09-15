"""Revoking a Vault lease (#1649), driven at the HTTP layer.

``httpx.MockTransport`` stands in for Vault so the real request building and
response classification run. The properties pinned here:

- it is ``PUT /v1/sys/leases/revoke`` with the lease id in the body — never in
  the URL, which the retry helper logs;
- a 2xx is revoked and a 400 (a lease Vault no longer holds) is done, not an
  error, since Vault versions answer either;
- a 4xx is final: exactly one request;
- a transient failure (5xx, connection error) is retried, a bounded number of
  times, then raised as ``VaultUnavailable``;
- no exception message carries the lease id.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from terrapod.config import VaultInstanceConfig
from terrapod.http_retry import DEFAULT_RETRIES
from terrapod.services import vault_client
from terrapod.services.vault_client import (
    GONE,
    REVOKED,
    VaultDenied,
    VaultError,
    VaultUnavailable,
    reset_token_cache,
    revoke_lease,
)

LEASE_ID = "database/creds/ro/LEASE-ID-MUST-NOT-LEAK"


def _inst(**kw) -> VaultInstanceConfig:
    return VaultInstanceConfig(
        name="default",
        address="https://vault.test:8200",
        auth={"method": "token", "mount": "token", "role": "n/a"},
        **kw,
    )


class _Vault:
    """Answers every request with ``status`` (or raises ``exc``) and records it."""

    def __init__(self, status: int = 204, exc: Exception | None = None):
        self.status = status
        self.exc = exc
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status)

    def serving(self):
        real = httpx.AsyncClient
        reset_token_cache()
        return patch.object(
            vault_client.httpx,
            "AsyncClient",
            lambda *a, **kw: real(transport=httpx.MockTransport(self)),
        )


@pytest.fixture(autouse=True)
def _no_backoff():
    # The retry helper's backoff is real sleep; the bound is what is under test.
    with patch("terrapod.http_retry.asyncio.sleep", AsyncMock()):
        yield


async def _revoke(vault: _Vault, **kw):
    with vault.serving():
        return await revoke_lease(_inst(**kw), LEASE_ID, static_token="tok")


class TestTheRequest:
    async def test_is_a_put_to_sys_leases_revoke_with_the_id_in_the_body(self):
        vault = _Vault(204)
        assert await _revoke(vault) == REVOKED
        (req,) = vault.requests
        assert req.method == "PUT"
        assert req.url.path == "/v1/sys/leases/revoke"
        assert LEASE_ID not in str(req.url)
        assert req.read() == b'{"lease_id":"' + LEASE_ID.encode() + b'"}'
        assert req.headers["X-Vault-Token"] == "tok"
        assert "X-Vault-Namespace" not in req.headers

    async def test_carries_the_namespace(self):
        vault = _Vault(204)
        await _revoke(vault, namespace="team-a")
        assert vault.requests[0].headers["X-Vault-Namespace"] == "team-a"

    async def test_an_empty_lease_id_makes_no_request(self):
        vault = _Vault(204)
        with vault.serving(), pytest.raises(VaultError):
            await revoke_lease(_inst(), "", static_token="tok")
        assert vault.requests == []


class TestTLS:
    async def test_revocation_verifies_tls_the_way_reads_do(self):
        """An instance with its own CA (#1650) must be revocable, so the revoke
        client takes ``verify`` from the same per-instance helper as a read."""
        vault = _Vault(204)
        pinned = object()
        seen: list = []
        real = httpx.AsyncClient

        def client(*a, **kw):
            seen.append(kw.get("verify"))
            return real(transport=httpx.MockTransport(vault))

        reset_token_cache()
        with (
            patch.object(vault_client, "_verify_for", AsyncMock(return_value=pinned)) as vf,
            patch.object(vault_client.httpx, "AsyncClient", client),
        ):
            assert await revoke_lease(_inst(), LEASE_ID, static_token="tok") == REVOKED
        vf.assert_awaited()
        assert seen == [pinned]


class TestOutcomes:
    @pytest.mark.parametrize("status", [200, 204])
    async def test_a_2xx_is_revoked(self, status):
        assert await _revoke(_Vault(status)) == REVOKED

    async def test_a_400_is_a_lease_vault_no_longer_holds_and_is_done(self):
        vault = _Vault(400)
        assert await _revoke(vault) == GONE
        assert len(vault.requests) == 1

    async def test_a_403_is_denied_final_and_names_the_missing_grant(self):
        vault = _Vault(403)
        with pytest.raises(VaultDenied) as ei:
            await _revoke(vault)
        assert len(vault.requests) == 1
        assert "sys/leases/revoke" in str(ei.value)
        assert LEASE_ID not in str(ei.value)

    @pytest.mark.parametrize("status", [401, 404, 405, 422])
    async def test_any_other_4xx_is_final(self, status):
        vault = _Vault(status)
        with pytest.raises(VaultError) as ei:
            await _revoke(vault)
        assert not isinstance(ei.value, VaultUnavailable)
        assert len(vault.requests) == 1
        assert LEASE_ID not in str(ei.value)

    async def test_a_5xx_is_retried_a_bounded_number_of_times(self):
        vault = _Vault(503)
        with pytest.raises(VaultUnavailable) as ei:
            await _revoke(vault)
        assert len(vault.requests) == DEFAULT_RETRIES + 1
        assert LEASE_ID not in str(ei.value)

    async def test_a_connection_error_is_retried_a_bounded_number_of_times(self):
        vault = _Vault(exc=httpx.ConnectError("refused"))
        with pytest.raises(VaultUnavailable) as ei:
            await _revoke(vault)
        assert len(vault.requests) == DEFAULT_RETRIES + 1
        assert LEASE_ID not in str(ei.value)
