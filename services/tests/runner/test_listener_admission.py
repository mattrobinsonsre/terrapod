"""Capacity-aware admission tests for the listener (#749).

The listener must gate run claims on the *real* running-Job count from K8s
(``count_active_runner_jobs``) plus in-flight launches, not just its
in-process ``_active_launches`` counter — which only tracks the brief Job
create window and let the listener over-admit onto a fixed-size cluster,
piling up Pending/unschedulable Jobs.

These build a listener with a mock-transport client and assert whether the
``GET .../runs/next`` claim is issued for a given occupancy.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from terrapod.runner.listener import RunnerListener


def _make_listener(client: httpx.AsyncClient) -> RunnerListener:
    listener = RunnerListener()
    listener._http_client = client
    listener.identity = SimpleNamespace(
        listener_id="11111111-1111-1111-1111-111111111111",
        name="listener",
        api_url="http://test",
        certificate_pem="",
    )
    return listener


def _counting_client(calls: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/next"):
            calls["next"] += 1
            return httpx.Response(204)  # no runs — stop cleanly
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(base_url="http://test", transport=transport)


@pytest.mark.asyncio
async def test_at_capacity_by_real_jobs_does_not_claim():
    """max_concurrent running Jobs → no claim even though _active_launches is 0."""
    calls = {"next": 0}
    async with _counting_client(calls) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 2
        listener._active_launches = 0
        with patch(
            "terrapod.runner.job_manager.count_active_runner_jobs",
            AsyncMock(return_value=2),
        ):
            await listener._handle_run_available()
    assert calls["next"] == 0  # gated on real occupancy


@pytest.mark.asyncio
async def test_free_capacity_claims():
    """Below capacity → the listener issues the claim GET."""
    calls = {"next": 0}
    async with _counting_client(calls) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 2
        listener._active_launches = 0
        with patch(
            "terrapod.runner.job_manager.count_active_runner_jobs",
            AsyncMock(return_value=1),
        ):
            await listener._handle_run_available()
    assert calls["next"] == 1


@pytest.mark.asyncio
async def test_launches_plus_jobs_sum_to_capacity_blocks():
    """In-flight launches + running Jobs together reaching the cap blocks a claim
    (the create window is counted so a Job mid-launch isn't double-admitted)."""
    calls = {"next": 0}
    async with _counting_client(calls) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 3
        listener._active_launches = 1
        with patch(
            "terrapod.runner.job_manager.count_active_runner_jobs",
            AsyncMock(return_value=2),
        ):
            await listener._handle_run_available()
    assert calls["next"] == 0


@pytest.mark.asyncio
async def test_launch_counter_alone_at_cap_skips_k8s_call():
    """The fast local guard short-circuits before the K8s list when launches
    alone already saturate the listener."""
    calls = {"next": 0}
    async with _counting_client(calls) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 1
        listener._active_launches = 1
        with patch(
            "terrapod.runner.job_manager.count_active_runner_jobs",
            AsyncMock(return_value=0),
        ) as mock_count:
            await listener._handle_run_available()
    assert calls["next"] == 0
    mock_count.assert_not_called()
