"""Tests for listener→API HTTP calls routing through the shared retry helper (#567).

The listener's discrete request/response API calls (heartbeat, job-status,
log-stream PUT, runner-token, etc.) go through ``arequest_with_retry`` so an
idempotent call survives a transient ReadTimeout instead of failing the cycle.

These tests use ``httpx.MockTransport`` + ``httpx.AsyncClient`` and patch the
retry helper's ``asyncio.sleep`` to an async no-op so the bounded backoff
doesn't actually delay the test.
"""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from terrapod.runner.listener import RunnerListener


def _make_listener(client: httpx.AsyncClient) -> RunnerListener:
    """Build a RunnerListener wired to a test client + stub identity.

    RunnerListener() is side-effect-light: load_runner_config() returns a
    default config when no runners.yaml exists, and the Prometheus metrics use
    a private registry. We then inject the mock-transport client and a minimal
    identity so the auth-header path and URL formatting work.
    """
    listener = RunnerListener()
    listener._http_client = client
    listener.identity = SimpleNamespace(
        listener_id="11111111-1111-1111-1111-111111111111",
        name="listener",
        api_url="http://test",
        certificate_pem="",  # empty cert → no X-Terrapod-Client-Cert header
    )
    return listener


@pytest.mark.asyncio
async def test_heartbeat_retries_past_read_timeout_and_succeeds():
    """An idempotent heartbeat POST retries past a ReadTimeout and then succeeds.

    The first transport attempt raises ReadTimeout (a transient, possibly-
    delivered failure). Because the call is marked idempotent=True, the shared
    helper retries; the second attempt returns 200. The listener method must
    complete without raising and record the heartbeat timestamp.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        listener = _make_listener(client)

        # Patch the helper's asyncio.sleep so backoff doesn't actually wait.
        with patch("terrapod.http_retry.asyncio.sleep") as sleep_mock:
            sleep_mock.return_value = None

            async def _noop(*_a, **_kw):
                return None

            sleep_mock.side_effect = _noop

            assert listener._last_heartbeat_at is None
            await listener._send_heartbeat()

    # Two transport attempts: the timed-out one and the successful retry.
    assert calls["n"] == 2
    # Backoff slept exactly once (between the two attempts).
    assert sleep_mock.await_count == 1
    # Success was observed — the heartbeat timestamp was recorded.
    assert listener._last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_heartbeat_read_timeout_exhausts_and_raises():
    """If every attempt times out, the last transport exception propagates.

    Confirms the helper doesn't silently swallow a persistent failure — the
    retry budget is bounded and the final ReadTimeout is re-raised so the
    listener's own try/except can log it.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("always times out", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        listener = _make_listener(client)

        with patch("terrapod.http_retry.asyncio.sleep") as sleep_mock:

            async def _noop(*_a, **_kw):
                return None

            sleep_mock.side_effect = _noop

            with pytest.raises(httpx.ReadTimeout):
                await listener._send_heartbeat()

    # Default 3 retries → 4 total attempts before giving up.
    assert calls["n"] == 4
    assert listener._last_heartbeat_at is None
