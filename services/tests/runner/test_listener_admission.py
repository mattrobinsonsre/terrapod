"""Capacity-aware admission tests for the listener (#749).

The listener must gate run claims on the *real* running-Job count from K8s
(``list_occupying_runner_jobs``) plus in-flight launches, not just its
in-process ``_active_launches`` counter — which only tracks the brief Job
create window and let the listener over-admit onto a fixed-size cluster,
piling up Pending/unschedulable Jobs.

These build a listener with a mock-transport client and assert whether the
``GET .../runs/next`` claim is issued for a given occupancy.
"""

import asyncio
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


def _names(n: int) -> set[str]:
    """`n` distinct occupying Job names, as the K8s list hands them over.

    Names rather than a bare count because the listener unions them with the
    Jobs it created itself this pass (#2399) — a count cannot be unioned.
    """
    return {f"tprun-occupied{i:04d}-plan" for i in range(n)}


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
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=_names(2)),
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
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=_names(1)),
        ):
            await listener._handle_run_available()
    assert calls["next"] == 1


@pytest.mark.asyncio
async def test_capacity_unknown_fails_closed():
    """K8s occupancy unknown (count returns None after retries) → the listener
    defers the whole pass and does NOT claim. This is the fail-closed guard that
    stops a transient apiserver blip from triggering an over-launch storm."""
    calls = {"next": 0}
    async with _counting_client(calls) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 10  # plenty of nominal headroom
        listener._active_launches = 0
        with patch(
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=None),
        ):
            await listener._handle_run_available()
    assert calls["next"] == 0  # deferred despite headroom — occupancy unknown


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
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=_names(2)),
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
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=_names(0)),
        ) as mock_count:
            await listener._handle_run_available()
    assert calls["next"] == 0
    mock_count.assert_not_called()


def _run_claim(n: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "id": f"run-{n:08d}-0000-0000-0000-000000000000",
                "attributes": {"phase": "plan"},
            }
        },
    )


@pytest.mark.asyncio
async def test_drains_backlog_in_one_pass_up_to_capacity():
    """A backlog drains in a single _handle_run_available pass, bounded by
    max_concurrent — even if the queue never empties (#750/#749). The
    `launched`-this-pass counter is what stops it (K8s count lags the just-
    created Jobs)."""
    launches = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/next"):
            launches["n"] += 1
            return _run_claim(launches["n"])  # queue never empties
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 3
        listener._active_launches = 0
        listener._launch_run = AsyncMock()
        with patch(
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=_names(0)),
        ):
            await listener._handle_run_available()

    assert listener._launch_run.await_count == 3  # capped at max_concurrent


@pytest.mark.asyncio
async def test_drain_stops_on_empty_queue_before_capacity():
    """The loop stops as soon as the pool returns 204, even below capacity."""
    responses = [_run_claim(1), _run_claim(2), httpx.Response(204)]
    idx = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/next"):
            r = responses[idx["i"]]
            idx["i"] += 1
            return r
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 5
        listener._active_launches = 0
        listener._launch_run = AsyncMock()
        with patch(
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            AsyncMock(return_value=_names(0)),
        ):
            await listener._handle_run_available()

    assert listener._launch_run.await_count == 2  # drained the 2 queued, then 204


# ── #2399: the burst the sequential tests above cannot see ──────────────────
#
# Every test above drives `_handle_run_available()` ONCE. Production never
# does: `_sse_loop` dispatches each event as its own task and the API publishes
# one `run_available` per run entering `queued`, so queueing N runs starts N
# drain passes at once. A race written sequentially is not a race, which is why
# a cap of 50 admitted ~146 runs in the field.


def _queue_client(served: dict, depth: int) -> httpx.AsyncClient:
    """Serves `depth` runs then 204, so a drain pass ends the way a real one does."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/next"):
            if served["n"] >= depth:
                return httpx.Response(204)
            served["n"] += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": f"run-{served['n']:08d}-0000-7000-8000-000000000000",
                        "attributes": {"phase": "plan"},
                    }
                },
            )
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))


def _lagging_list(created: list, lag: int):
    """A K8s list that has not yet caught up with the `lag` newest Jobs.

    `lag` is the window the old code counted with nothing at all: between
    `create_namespaced_job` returning and the Job controller creating the pod,
    `status.active` is 0 and `_active_launches` has already been released.
    Every fake here awaits, because a fake with no await inside runs
    start-to-finish in one slice and never interleaves — a race that cannot
    happen is not being tested.
    """

    async def fake(namespace: str = "", **kw) -> set[str]:
        await asyncio.sleep(0)
        return set(created[: max(0, len(created) - lag)])

    return fake


async def _burst(max_concurrent: int, events: int, depth: int, lag: int) -> list[str]:
    """Run `events` concurrent drain passes; return the Job names launched."""
    from terrapod.runner.job_template import runner_job_name

    served = {"n": 0}
    created: list[str] = []
    async with _queue_client(served, depth) as client:
        listener = _make_listener(client)
        listener._max_concurrent = max_concurrent

        async def fake_launch(run_id, attrs):
            created.append(runner_job_name(run_id, attrs.get("phase", "plan")))
            await asyncio.sleep(0)

        listener._launch_run = AsyncMock(side_effect=fake_launch)
        with patch(
            "terrapod.runner.job_manager.list_occupying_runner_jobs",
            _lagging_list(created, lag),
        ):
            await asyncio.gather(*(listener._handle_run_available() for _ in range(events)))
    return created


@pytest.mark.asyncio
@pytest.mark.parametrize("lag", [0, 1, 5, 20, 999])
async def test_a_burst_of_concurrent_events_never_exceeds_the_cap(lag):
    """The reported failure: 146 runs queued at once against a cap of 50.

    Parametrised on the lag because that is the axis the defect lived on — at
    lag 0 the old code held the cap too, so a test that only ran that case
    would have passed throughout. `lag=999` is the degenerate worst case where
    the list never catches up at all.
    """
    created = await _burst(max_concurrent=50, events=146, depth=146, lag=lag)
    assert len(created) <= 50, f"over-admitted {len(created)} Jobs against a cap of 50"


@pytest.mark.asyncio
async def test_a_burst_still_fills_the_pool_rather_than_under_admitting():
    """The cap is a ceiling to reach, not to hide behind.

    Pairs with the test above: clamping admission to something far below
    `max_concurrent` would satisfy that assertion while making the pool slow,
    which is the easy wrong fix.
    """
    created = await _burst(max_concurrent=50, events=146, depth=146, lag=5)
    assert len(created) == 50


@pytest.mark.asyncio
async def test_a_short_queue_drains_completely_under_a_burst():
    """A backlog smaller than the cap is drained, not left sitting (#750)."""
    created = await _burst(max_concurrent=50, events=146, depth=7, lag=999)
    assert len(created) == 7


@pytest.mark.asyncio
async def test_a_job_created_this_pass_counts_before_kubernetes_lists_it():
    """The window that was counted by nothing.

    With the list permanently empty, occupancy can only come from what this
    pass created — so the cap holding here is the union doing the work.
    """
    created = await _burst(max_concurrent=3, events=1, depth=20, lag=999)
    assert len(created) == 3


@pytest.mark.asyncio
async def test_a_pass_that_stopped_at_capacity_does_not_immediately_redrain():
    """Re-draining on a pending wakeup must not restart a blind pass.

    A fresh pass has no memory of the Jobs the previous one created, so if the
    pool is full, looping again reads a stale list and admits a second cohort
    — the other half of #2399. Nothing has freed, and a slot freeing is itself
    an event (the API publishes `run_available` on every terminal transition).
    """
    created = await _burst(max_concurrent=3, events=12, depth=50, lag=999)
    assert len(created) == 3


@pytest.mark.asyncio
async def test_a_wakeup_arriving_during_a_drain_is_not_lost():
    """Coalescing must not drop work, only duplication.

    A run queued while a pass is mid-drain has to be picked up by that pass
    going round again — otherwise the fix for over-admission would trade it
    for runs sitting in `queued` until the 30s poll.
    """
    served = {"n": 0}
    calls = {"next": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/next"):
            calls["next"] += 1
            # Empty on the first pass; a run "arrives" while that pass runs.
            if calls["next"] == 1:
                return httpx.Response(204)
            if served["n"] >= 1:
                return httpx.Response(204)
            served["n"] += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-00000001-0000-7000-8000-000000000000",
                        "attributes": {"phase": "plan"},
                    }
                },
            )
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        listener = _make_listener(client)
        listener._max_concurrent = 5
        listener._launch_run = AsyncMock()

        async def empty_list(namespace: str = "", **kw) -> set[str]:
            await asyncio.sleep(0)
            return set()

        with patch("terrapod.runner.job_manager.list_occupying_runner_jobs", empty_list):
            # Two events: the first drains an empty queue, the second is the
            # wakeup that must still be honoured.
            await asyncio.gather(listener._handle_run_available(), listener._handle_run_available())

    assert listener._launch_run.await_count == 1, "the second wakeup was dropped"


@pytest.mark.asyncio
@pytest.mark.parametrize("replicas", [1, 2, 3, 4, 6])
async def test_several_replicas_sharing_a_namespace_stay_within_one_job_per_extra(replicas):
    """Replicas coordinate only through the namespace-wide list, so bound it.

    `listener.maxConcurrent` is documented as a POOL-wide ceiling, which means
    this is the number an operator running more than one replica relies on.
    Measured against a real apiserver: a list with no `resourceVersion` is a
    consistent read, so a Job is visible to every replica the moment its
    creation returns. What is left is the window between one replica deciding
    it has room and its Job existing — one Job per extra replica, which is why
    two replicas hold the cap exactly.

    The parametrisation is the point: asserting only `<= max * replicas` would
    pass on the pre-fix code.
    """
    from terrapod.runner.job_template import runner_job_name

    max_concurrent, depth = 10, 60
    served = {"n": 0}
    created: list[str] = []  # the shared namespace

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/next"):
            if served["n"] >= depth:
                return httpx.Response(204)
            served["n"] += 1
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": f"run-{served['n']:08d}-0000-7000-8000-000000000000",
                        "attributes": {"phase": "plan"},
                    }
                },
            )
        return httpx.Response(200, json={"ok": True})

    async def shared_list(namespace: str = "", **kw) -> set[str]:
        await asyncio.sleep(0)
        return set(created)

    async def fake_launch(run_id, attrs):
        # A Job exists only once its creation RETURNS; the awaits are the
        # claim/token/Secret/create round trips before that, and they are what
        # opens the one window replicas cannot see into.
        for _ in range(4):
            await asyncio.sleep(0)
        created.append(runner_job_name(run_id, attrs.get("phase", "plan")))

    clients, passes = [], []
    try:
        for r in range(replicas):
            client = httpx.AsyncClient(
                base_url="http://test", transport=httpx.MockTransport(handler)
            )
            clients.append(client)
            listener = _make_listener(client)
            listener.identity.listener_id = f"{r:08d}-1111-1111-1111-111111111111"
            listener._max_concurrent = max_concurrent
            listener._launch_run = AsyncMock(side_effect=fake_launch)
            passes.extend(listener._handle_run_available() for _ in range(12))

        with patch("terrapod.runner.job_manager.list_occupying_runner_jobs", shared_list):
            await asyncio.gather(*passes)
    finally:
        for client in clients:
            await client.aclose()

    # Derived, not tabulated: each replica can be at most one claim-and-create
    # window ahead of what the shared list justified, so the pool can exceed
    # the cap by at most one Job per EXTRA replica. A single replica is exact.
    allowed_over = replicas - 1
    assert len(created) <= max_concurrent + allowed_over, (
        f"{replicas} replicas launched {len(created)} against a cap of {max_concurrent} "
        f"(at most {allowed_over} over is expected)"
    )
