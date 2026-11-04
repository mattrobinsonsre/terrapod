"""Tests for the shared method-aware HTTP retry helper (#567).

Proves the safety property that distinguishes this from a naive "retry
everything": a non-idempotent POST is NOT retried on a read-timeout or 5xx
(which could have been delivered/applied), only on a connection error where
the request provably never reached the server.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from terrapod import http_retry


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(http_retry.time, "sleep", lambda *_a, **_k: None)

    async def _async_noop(*_a, **_k):
        return None

    monkeypatch.setattr(http_retry.asyncio, "sleep", _async_noop)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _aclient(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_PRIVATE_URL = "https://user:password@mirror.example/index.json?token=index-secret"


@pytest.mark.parametrize("failure", ["transport", "status"])
def test_sync_retry_logs_no_credentials(failure):
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "transport":
                raise httpx.ReadTimeout(f"failed at {req.url}", request=req)
            return httpx.Response(503)
        return httpx.Response(200)

    with patch.object(http_retry.logger, "warning") as warning, _client(handler) as client:
        assert http_retry.request_with_retry(client, "GET", _PRIVATE_URL).status_code == 200

    assert calls == 2
    warning.assert_called_once()
    fields = warning.call_args.kwargs
    assert fields["method"] == "GET"
    if failure == "transport":
        assert fields["err"] == "ReadTimeout"
    else:
        assert fields["status"] == 503
    assert _PRIVATE_URL not in str(warning.call_args)
    assert "password" not in str(warning.call_args)
    assert "index-secret" not in str(warning.call_args)


@pytest.mark.parametrize("failure", ["transport", "status"])
async def test_async_retry_logs_no_credentials(failure):
    calls = 0

    def handler(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "transport":
                raise httpx.ConnectError(f"failed at {req.url}", request=req)
            return httpx.Response(502)
        return httpx.Response(200)

    with patch.object(http_retry.logger, "warning") as warning:
        async with _aclient(handler) as client:
            resp = await http_retry.arequest_with_retry(client, "GET", _PRIVATE_URL)
    assert resp.status_code == 200

    assert calls == 2
    warning.assert_called_once()
    fields = warning.call_args.kwargs
    assert fields["method"] == "GET"
    if failure == "transport":
        assert fields["err"] == "ConnectError"
    else:
        assert fields["status"] == 502
    assert _PRIVATE_URL not in str(warning.call_args)
    assert "password" not in str(warning.call_args)
    assert "index-secret" not in str(warning.call_args)


def test_redact_urls_preserves_safe_diagnostics():
    message = f"tofu 1.9.0: {_PRIVATE_URL} returned 404. Pin a version with an artifact."
    assert http_retry.redact_urls(message) == (
        "tofu 1.9.0: [redacted URL] returned 404. Pin a version with an artifact."
    )
    assert "it's-secret" not in http_retry.redact_urls(
        "index https://mirror.example/index.json?token=it's-secret returned 503"
    )


def test_exception_diagnostic_preserves_upstream_status_without_url():
    request = httpx.Request("GET", _PRIVATE_URL)
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError(
        f"bad response from {_PRIVATE_URL}", request=request, response=response
    )
    assert http_retry.exception_diagnostic(exc) == "HTTPStatusError (upstream HTTP 503)"


class TestSyncRetry:
    def test_idempotent_get_retries_timeout_then_succeeds(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("timed out", request=req)
            return httpx.Response(200)

        with _client(handler) as c:
            resp = http_retry.request_with_retry(c, "GET", "https://x/y")
        assert resp.status_code == 200
        assert calls["n"] == 2

    def test_nonidempotent_post_not_retried_on_read_timeout(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out", request=req)

        with _client(handler) as c, pytest.raises(httpx.ReadTimeout):
            http_retry.request_with_retry(c, "POST", "https://x/y")
        assert calls["n"] == 1  # read-timeout on POST may be delivered → no retry

    def test_nonidempotent_post_retried_on_connect_error(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("refused", request=req)
            return httpx.Response(201)

        with _client(handler) as c:
            resp = http_retry.request_with_retry(c, "POST", "https://x/y")
        assert resp.status_code == 201
        assert calls["n"] == 2  # connect error = not delivered → safe to retry

    def test_idempotent_put_retries_5xx(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return httpx.Response(503 if calls["n"] == 1 else 200)

        with _client(handler) as c:
            resp = http_retry.request_with_retry(c, "PUT", "https://x/y")
        assert resp.status_code == 200
        assert calls["n"] == 2

    def test_nonidempotent_post_not_retried_on_5xx(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return httpx.Response(500)

        with _client(handler) as c:
            resp = http_retry.request_with_retry(c, "POST", "https://x/y")
        assert resp.status_code == 500
        assert calls["n"] == 1  # 5xx on POST may have applied → don't retry

    def test_explicit_idempotent_post_retries_5xx(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return httpx.Response(500 if calls["n"] == 1 else 204)

        with _client(handler) as c:
            resp = http_retry.request_with_retry(c, "POST", "https://x/y", idempotent=True)
        assert resp.status_code == 204
        assert calls["n"] == 2

    def test_4xx_is_final(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            return httpx.Response(422)

        with _client(handler) as c:
            resp = http_retry.request_with_retry(c, "GET", "https://x/y")
        assert resp.status_code == 422
        assert calls["n"] == 1

    def test_gives_up_after_retries(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise httpx.ReadTimeout("t", request=req)

        with _client(handler) as c, pytest.raises(httpx.ReadTimeout):
            http_retry.request_with_retry(c, "GET", "https://x/y", retries=3)
        assert calls["n"] == 4  # 1 initial + 3 retries


class TestAsyncRetry:
    async def test_idempotent_get_retries_timeout(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("t", request=req)
            return httpx.Response(200)

        async with _aclient(handler) as c:
            resp = await http_retry.arequest_with_retry(c, "GET", "https://x/y")
        assert resp.status_code == 200
        assert calls["n"] == 2

    async def test_nonidempotent_post_not_retried_on_timeout(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise httpx.ReadTimeout("t", request=req)

        async with _aclient(handler) as c:
            with pytest.raises(httpx.ReadTimeout):
                await http_retry.arequest_with_retry(c, "POST", "https://x/y")
        assert calls["n"] == 1

    async def test_post_retried_on_connect_error(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("x", request=req)
            return httpx.Response(201)

        async with _aclient(handler) as c:
            resp = await http_retry.arequest_with_retry(c, "POST", "https://x/y")
        assert resp.status_code == 201
        assert calls["n"] == 2

    async def test_idempotent_get_gives_up(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            raise httpx.ConnectError("x", request=req)

        async with _aclient(handler) as c:
            with pytest.raises(httpx.ConnectError):
                await http_retry.arequest_with_retry(c, "GET", "https://x/y", retries=2)
        assert calls["n"] == 3  # 1 + 2
