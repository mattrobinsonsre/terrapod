"""The no-progress detector for a long-running configure (#1977).

The property under test is **content change**, not arrival. The reconciler
publishes a `stream_logs` event every cycle and the listener answers it whether
or not ansible produced anything, so the live snapshot is rewritten roughly
every ten seconds regardless -- a detector keyed on "did an upload arrive"
would report every run healthy for ever. Each test below therefore holds the
snapshot's CONTENT fixed or changes it, never its arrival.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import run_reconciler


def _run(*, is_configure=True, status="configuring"):
    return SimpleNamespace(
        id="01a00000-0000-0000-0000-000000000001",
        is_configure=is_configure,
        status=status,
    )


class _Redis:
    """Records what the detector stored, so a test can prove the clock is not reset."""

    def __init__(self, log: bytes | None, marker: str | None = None):
        self._log = log
        self._marker = marker
        self.setex_calls: list[tuple[str, int, str]] = []

    async def get(self, key: str):
        if key.startswith("tp:log_stream:"):
            return self._log
        return self._marker

    async def setex(self, key: str, ttl: int, value: str):
        self.setex_calls.append((key, ttl, value))


def _cfg(window=1800):
    return SimpleNamespace(configure_no_progress_seconds=window)


async def _call(redis, run, *, window=1800):
    failed = AsyncMock()
    reaped = AsyncMock()
    with (
        patch("terrapod.redis.client.get_redis_client", return_value=redis),
        patch.object(run_reconciler, "load_runner_config", return_value=_cfg(window)),
        patch.object(run_reconciler, "_handle_failed", failed),
        patch.object(run_reconciler, "_reap_abandoned_job", reaped),
    ):
        await run_reconciler._check_configure_progress(AsyncMock(), run, "configure")
    return failed, reaped


@pytest.mark.asyncio
async def test_unchanged_output_beyond_the_window_errors_the_run():
    """The whole point: same bytes, long enough, so nothing is happening."""
    stale = f"deadbeefdeadbeefdeadbeefdeadbeef:{time.time() - 4000}"
    # The marker's digest has to match what the detector computes, so compute it
    # the way the detector does rather than guessing.
    import hashlib

    body = b"TASK [wedged] ***\n"
    digest = hashlib.sha256(body).hexdigest()[:32]
    stale = f"{digest}:{time.time() - 4000}"
    failed, reaped = await _call(_Redis(body, stale), _run())
    assert failed.await_count == 1
    assert reaped.await_count == 1
    msg = failed.await_args.args[2]
    assert "no output" in msg and "1800" in msg


@pytest.mark.asyncio
async def test_unchanged_but_still_inside_the_window_does_not_fire():
    import hashlib

    body = b"TASK [slow but legitimate] ***\n"
    digest = hashlib.sha256(body).hexdigest()[:32]
    failed, _ = await _call(_Redis(body, f"{digest}:{time.time() - 10}"), _run())
    assert failed.await_count == 0


@pytest.mark.asyncio
async def test_unchanged_does_NOT_rewrite_the_marker():
    """Rewriting it every tick would reset the clock and the window could never
    elapse -- the detector would be permanently inert while looking correct."""
    import hashlib

    body = b"TASK [same] ***\n"
    digest = hashlib.sha256(body).hexdigest()[:32]
    redis = _Redis(body, f"{digest}:{time.time() - 10}")
    await _call(redis, _run())
    assert redis.setex_calls == []


@pytest.mark.asyncio
async def test_changed_output_restarts_the_clock():
    """New bytes against an OLD marker: progress, so it must not fire even
    though the stored timestamp is long past the window."""
    redis = _Redis(b"TASK [something new] ***\n", f"{'0' * 32}:{time.time() - 9999}")
    failed, _ = await _call(redis, _run())
    assert failed.await_count == 0
    assert len(redis.setex_calls) == 1
    assert redis.setex_calls[0][1] >= 1800


@pytest.mark.asyncio
async def test_an_absent_snapshot_is_not_no_progress():
    """It means the listener stopped answering, which is `_check_stale`'s
    failure. Reporting it here would name the wrong cause.

    **The marker holds the digest of EMPTY output deliberately.** An arbitrary
    digest makes this test vacuous: removing the guard then hashes `b""`, which
    does not match, so the detector takes the changed-output path and the test
    passes either way. Mutation-checked -- with an arbitrary digest it survives
    `live = live or b""`, and with this one it does not.
    """
    import hashlib

    empty_digest = hashlib.sha256(b"").hexdigest()[:32]
    failed, _ = await _call(_Redis(None, f"{empty_digest}:{time.time() - 9999}"), _run())
    assert failed.await_count == 0


@pytest.mark.asyncio
async def test_a_window_of_zero_disables_it():
    import hashlib

    body = b"TASK [x] ***\n"
    digest = hashlib.sha256(body).hexdigest()[:32]
    failed, _ = await _call(_Redis(body, f"{digest}:{time.time() - 9999}"), _run(), window=0)
    assert failed.await_count == 0


@pytest.mark.asyncio
async def test_it_applies_to_a_configure_and_nothing_else():
    """A plan or apply reporting `running` reaches the same branch, and this
    must not bound it -- a long terraform plan legitimately prints nothing."""
    import hashlib

    body = b"still creating...\n"
    digest = hashlib.sha256(body).hexdigest()[:32]
    marker = f"{digest}:{time.time() - 9999}"
    for run in (
        _run(is_configure=False, status="applying"),
        _run(is_configure=True, status="applying"),
    ):
        failed, _ = await _call(_Redis(body, marker), run)
        assert failed.await_count == 0, run


def test_the_running_branch_calls_it_BEFORE_returning():
    """The call site, not the helper (#1977).

    A wedged configure reports `running`, so this branch is the only one it
    reaches -- `_check_stale` is called only when no status was reported at
    all. A helper that is never invoked from here is inert, and no behavioural
    test of the helper can see that.
    """
    import inspect

    src = inspect.getsource(run_reconciler)
    anchor = src.index('if status == "running":')
    tail = src[anchor : anchor + 600]
    call = tail.index("_check_configure_progress")
    ret = tail.index("return")
    assert call < ret, "the detector must run before the branch returns"


class TestLastOutputAt:
    """The read side of the same marker (#1977).

    This is what gives an operator the distinction the issue asks for: "stuck"
    is now an errored run carrying the silence in its message, and "quiet for
    twenty minutes" is a run still going whose last output was a while ago.
    """

    @pytest.mark.asyncio
    async def test_it_returns_when_the_output_last_changed(self):
        at = time.time() - 300
        redis = _Redis(None, f"{'a' * 32}:{at}")
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            got = await run_reconciler.last_output_at("run", "configure")
        assert got is not None
        assert abs(got.timestamp() - at) < 1

    @pytest.mark.asyncio
    async def test_an_absent_marker_is_None_rather_than_a_guess(self):
        redis = _Redis(None, None)
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            assert await run_reconciler.last_output_at("run", "configure") is None

    @pytest.mark.asyncio
    async def test_a_malformed_marker_is_None_rather_than_a_crash(self):
        redis = _Redis(None, "not-a-marker")
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            assert await run_reconciler.last_output_at("run", "configure") is None

    @pytest.mark.asyncio
    async def test_a_redis_failure_cannot_fail_a_run_read(self):
        """It is surfaced on `GET /runs/{id}`, so a Redis outage must degrade
        this one attribute rather than break every run page."""

        class _Broken:
            async def get(self, _key):
                raise ConnectionError("redis down")

        with patch("terrapod.redis.client.get_redis_client", return_value=_Broken()):
            assert await run_reconciler.last_output_at("run", "configure") is None


def test_the_attribute_is_surfaced_ONLY_while_a_configure_is_in_flight():
    """Runtime adaptation rather than a flag, per #1977's own requirement that a
    terraform-only deployment pays nothing: the key is absent from every run
    read that is not a live configure, and the Redis GET with it.

    Positional, because a behavioural test would have to drive `show_run` for
    every status and assert an absence.
    """
    import inspect

    from terrapod.api.routers import runs as runs_router

    src = inspect.getsource(runs_router.show_run)
    assert src.count('"last-output-at"') == 1
    guard = src.index('if run.status == "configuring":')
    assert src.index('"last-output-at"') > guard, (
        "set outside the configuring guard, so every run read pays a Redis GET"
    )
    assert src.index("last_output_at") > guard, "the Redis read is not gated either"
