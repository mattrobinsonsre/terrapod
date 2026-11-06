"""Tests for listener→API HTTP calls routing through the shared retry helper (#567).

The listener's discrete request/response API calls (heartbeat, job-status,
log-stream PUT, runner-token, etc.) go through ``arequest_with_retry`` so an
idempotent call survives a transient ReadTimeout instead of failing the cycle.

These tests use ``httpx.MockTransport`` + ``httpx.AsyncClient`` and patch the
retry helper's ``asyncio.sleep`` to an async no-op so the bounded backoff
doesn't actually delay the test.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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


# ── #2406: the heartbeat's occupancy report has a call site, not just a helper ──
#
# `_send_heartbeat` is what tells the API how busy a listener is, and #2403
# changed what that number counts. Three mutations of the call site survived
# the whole suite before these existed: deleting the occupancy read (every
# heartbeat reports 0), dropping the `is not None` guard (a Kubernetes blip
# sends `null`, which the code's own comment says must not happen), and
# dropping `retries=1` (the heartbeat then pays admission-grade retry latency
# on a blip). Testing the helper directly cannot see any of them.


def _capture_heartbeat(bodies: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/heartbeat"):
            bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_heartbeat_reports_the_occupancy_it_measured():
    bodies: list = []
    async with _capture_heartbeat(bodies) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 7
        with patch(
            "terrapod.runner.job_manager.count_active_runner_jobs",
            AsyncMock(return_value=4),
        ):
            await listener._send_heartbeat()

    assert len(bodies) == 1
    assert bodies[0]["active_runs"] == 4, bodies[0]
    assert bodies[0]["capacity"] == 7, bodies[0]


@pytest.mark.asyncio
async def test_an_unknown_occupancy_keeps_the_last_value_rather_than_reporting_none():
    """A Kubernetes blip must not publish `null` as the occupancy.

    The listener's own comment says so: the figure is best-effort status that
    self-heals on the next heartbeat, so an unknown reading keeps the last
    one. Dropping the `is not None` guard sends `null` to the API, and nothing
    noticed.
    """
    bodies: list = []
    async with _capture_heartbeat(bodies) as client:
        listener = _make_listener(client)
        with patch(
            "terrapod.runner.job_manager.count_active_runner_jobs",
            AsyncMock(return_value=3),
        ):
            await listener._send_heartbeat()
        with patch(
            "terrapod.runner.job_manager.count_active_runner_jobs",
            AsyncMock(return_value=None),  # occupancy unknown
        ):
            await listener._send_heartbeat()

    assert [b["active_runs"] for b in bodies] == [3, 3], bodies


@pytest.mark.asyncio
async def test_the_heartbeat_does_not_pay_admission_grade_retries_for_occupancy():
    """`retries=1`, because this figure is status rather than a decision.

    Admission fails closed and retries hard when occupancy is unknown, since
    it is about to launch Jobs. The heartbeat must not: it would add backoff
    latency to every heartbeat during an apiserver blip, for a number that
    self-heals on the next one.
    """
    seen: dict = {}

    async def fake_count(namespace: str = "", **kw):
        seen.update(namespace=namespace, **kw)
        return 2

    bodies: list = []
    async with _capture_heartbeat(bodies) as client:
        listener = _make_listener(client)
        with patch("terrapod.runner.job_manager.count_active_runner_jobs", fake_count):
            await listener._send_heartbeat()

    assert seen.get("retries") == 1, seen
