"""Retry behaviour of API-server outbound HTTP via the shared helper (#567).

These exercise the two semantic halves the API server relies on when it
routes its upstream fetches (provider/binary/platform-provider caches) and
its outbound webhook deliveries (notifications, run-task callbacks) through
``arequest_with_retry``:

(a) An upstream GET is idempotent by method, so a transient ``ReadTimeout``
    is retried and a subsequent success is returned.
(b) A webhook POST is non-idempotent (default), so a ``ReadTimeout`` — which
    may mean the request was already delivered — is NOT retried; re-POSTing
    would double-deliver. Only a connection-not-sent error retries a POST.

Both use ``httpx.MockTransport`` so no socket is opened, and patch
``terrapod.http_retry.asyncio.sleep`` so the backoff doesn't actually wait.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from terrapod.http_retry import arequest_with_retry


async def test_idempotent_get_retries_past_read_timeout_and_succeeds():
    """An upstream GET retries a ReadTimeout and returns the eventual 200."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # First attempt: the upstream read times out mid-response.
            raise httpx.ReadTimeout("simulated upstream read timeout", request=request)
        return httpx.Response(200, json={"versions": []})

    transport = httpx.MockTransport(handler)
    with patch("terrapod.http_retry.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await arequest_with_retry(
                client, "GET", "https://registry.example.com/v1/providers/foo/bar/versions"
            )

    assert resp.status_code == 200
    # First attempt timed out, second attempt succeeded.
    assert calls["n"] == 2
    # Backoff slept exactly once (before the single retry).
    sleep_mock.assert_awaited_once()


async def test_webhook_post_does_not_retry_on_read_timeout():
    """A non-idempotent webhook POST is NOT retried on a ReadTimeout.

    The first attempt may already have been delivered, so re-POSTing would
    double-deliver. The helper re-raises the ReadTimeout after one attempt.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("simulated slow webhook receiver", request=request)

    transport = httpx.MockTransport(handler)
    with patch("terrapod.http_retry.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ReadTimeout):
                await arequest_with_retry(
                    client,
                    "POST",
                    "https://hooks.example.com/webhook",
                    content=b'{"hello": "world"}',
                )

    # Exactly one attempt — no retry on a read-timeout for a non-idempotent POST.
    assert calls["n"] == 1
    sleep_mock.assert_not_awaited()


async def test_webhook_post_retries_on_connect_error():
    """A webhook POST DOES retry on a connection-not-sent error.

    A ``ConnectError`` proves the request never reached the receiver, so a
    retry can't double-deliver — the one transient case a non-idempotent POST
    is allowed to retry.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    with patch("terrapod.http_retry.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await arequest_with_retry(
                client,
                "POST",
                "https://hooks.example.com/webhook",
                content=b'{"hello": "world"}',
            )

    assert resp.status_code == 204
    assert calls["n"] == 2
    sleep_mock.assert_awaited_once()
