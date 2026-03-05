"""Tests for the distributed task scheduler."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from terrapod.services.scheduler import (
    PREFIX,
    PeriodicTaskDef,
    TriggerHandlerDef,
    _clear_dedup,
    _run_periodic_loop,
    _run_trigger_consumer,
    enqueue_trigger,
    get_last_run,
    mark_completed,
    try_claim_periodic,
)

# ── try_claim_periodic ────────────────────────────────────────────────


class TestTryClaimPeriodic:
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_claim_succeeds_when_no_lock(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.exists.return_value = 0
        redis.set.return_value = True

        result = await try_claim_periodic("test_task", 60)
        assert result is True
        # Should check running key
        redis.exists.assert_called_once_with(f"{PREFIX}:test_task:running")
        # Should set claim key with NX and EX
        redis.set.assert_any_call(f"{PREFIX}:test_task:claim", unittest_mock_any(), nx=True, ex=60)

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_claim_fails_when_already_running(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.exists.return_value = 1  # running key exists

        result = await try_claim_periodic("test_task", 60)
        assert result is False

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_claim_fails_when_lock_held(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.exists.return_value = 0  # not running
        redis.set.return_value = None  # NX failed — another replica holds lock

        result = await try_claim_periodic("test_task", 60)
        assert result is False

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_claim_sets_running_key_with_3x_ttl(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.exists.return_value = 0
        redis.set.return_value = True

        await try_claim_periodic("test_task", 60)
        # Second set call should be the running key with 3x TTL
        calls = redis.set.call_args_list
        running_call = [c for c in calls if f"{PREFIX}:test_task:running" in str(c)]
        assert len(running_call) == 1
        assert running_call[0].kwargs.get("ex") == 180  # 60 * 3


# ── mark_completed ────────────────────────────────────────────────────


class TestMarkCompleted:
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_deletes_running_key_and_sets_last(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        await mark_completed("test_task")
        redis.delete.assert_called_once_with(f"{PREFIX}:test_task:running")
        redis.set.assert_called_once()
        set_args = redis.set.call_args
        assert set_args[0][0] == f"{PREFIX}:test_task:last"


# ── get_last_run ──────────────────────────────────────────────────────


class TestGetLastRun:
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_returns_float_when_exists(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.get.return_value = "1700000000.0"

        result = await get_last_run("test_task")
        assert result == 1700000000.0

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_returns_none_when_missing(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.get.return_value = None

        result = await get_last_run("test_task")
        assert result is None


# ── enqueue_trigger ───────────────────────────────────────────────────


class TestEnqueueTrigger:
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_enqueue_without_dedup(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        result = await enqueue_trigger("test_type", {"key": "value"})
        assert result is True
        redis.lpush.assert_called_once()
        pushed = json.loads(redis.lpush.call_args[0][1])
        assert pushed["type"] == "test_type"
        assert pushed["payload"] == {"key": "value"}
        assert pushed["dedup_key"] is None

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_enqueue_with_dedup_succeeds(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.set.return_value = True  # NX succeeded (not a duplicate)

        result = await enqueue_trigger("test_type", {"key": "value"}, dedup_key="my_dedup")
        assert result is True
        redis.set.assert_called_once_with(f"{PREFIX}:trigger:my_dedup", "1", nx=True, ex=300)
        redis.lpush.assert_called_once()

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_enqueue_with_dedup_rejected(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.set.return_value = None  # NX failed (duplicate)

        result = await enqueue_trigger("test_type", {"key": "value"}, dedup_key="my_dedup")
        assert result is False
        redis.lpush.assert_not_called()

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_custom_dedup_ttl(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.set.return_value = True

        await enqueue_trigger("t", dedup_key="k", dedup_ttl=600)
        redis.set.assert_called_once_with(f"{PREFIX}:trigger:k", "1", nx=True, ex=600)

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_empty_payload_defaults_to_empty_dict(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        await enqueue_trigger("t")
        pushed = json.loads(redis.lpush.call_args[0][1])
        assert pushed["payload"] == {}


# ── _clear_dedup ──────────────────────────────────────────────────────


class TestClearDedup:
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_deletes_key_when_provided(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        await _clear_dedup("my_key")
        redis.delete.assert_called_once_with(f"{PREFIX}:trigger:my_key")

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_noop_when_none(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        await _clear_dedup(None)
        redis.delete.assert_not_called()


# ── _run_periodic_loop ────────────────────────────────────────────────


class TestRunPeriodicLoop:
    @patch("terrapod.services.scheduler.mark_completed")
    @patch("terrapod.services.scheduler.try_claim_periodic")
    async def test_executes_handler_when_claimed(self, mock_claim, mock_complete):
        mock_claim.return_value = True
        handler = AsyncMock()
        task_def = PeriodicTaskDef("test", 1, handler)
        shutdown = asyncio.Event()

        async def stop_after_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.create_task(stop_after_one())
        await _run_periodic_loop(task_def, shutdown)

        handler.assert_called()
        mock_complete.assert_called_with("test")

    @patch("terrapod.services.scheduler.mark_completed")
    @patch("terrapod.services.scheduler.try_claim_periodic")
    async def test_skips_when_not_claimed(self, mock_claim, mock_complete):
        mock_claim.return_value = False
        handler = AsyncMock()
        task_def = PeriodicTaskDef("test", 1, handler)
        shutdown = asyncio.Event()

        async def stop_after_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.create_task(stop_after_one())
        await _run_periodic_loop(task_def, shutdown)

        handler.assert_not_called()
        mock_complete.assert_not_called()

    @patch("terrapod.services.scheduler.mark_completed")
    @patch("terrapod.services.scheduler.try_claim_periodic")
    async def test_handler_exception_still_marks_completed(self, mock_claim, mock_complete):
        mock_claim.return_value = True
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        task_def = PeriodicTaskDef("test", 1, handler)
        shutdown = asyncio.Event()

        async def stop_after_one():
            await asyncio.sleep(0.05)
            shutdown.set()

        asyncio.create_task(stop_after_one())
        await _run_periodic_loop(task_def, shutdown)

        handler.assert_called()
        mock_complete.assert_called_with("test")


# ── _run_trigger_consumer ─────────────────────────────────────────────


class TestRunTriggerConsumer:
    @patch("terrapod.services.scheduler._clear_dedup")
    @patch("terrapod.services.scheduler._trigger_handlers", {})
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_dispatches_to_registered_handler(self, mock_get_redis, mock_clear):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        handler = AsyncMock()

        # Register handler
        from terrapod.services.scheduler import _trigger_handlers

        _trigger_handlers["test_type"] = TriggerHandlerDef("test_type", handler)

        item = json.dumps(
            {
                "type": "test_type",
                "payload": {"repo": "org/repo"},
                "dedup_key": "dk",
                "enqueued_at": 0,
            }
        )
        shutdown = asyncio.Event()
        call_count = 0

        async def fake_brpop(key, timeout=0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (key, item)
            # Signal shutdown after first item processed
            shutdown.set()
            return None

        redis.brpop = fake_brpop

        await _run_trigger_consumer(shutdown)

        handler.assert_called_once_with({"repo": "org/repo"})
        mock_clear.assert_called_once_with("dk")

    @patch("terrapod.services.scheduler._clear_dedup")
    @patch("terrapod.services.scheduler._trigger_handlers", {})
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_unknown_type_clears_dedup(self, mock_get_redis, mock_clear):
        redis = AsyncMock()
        mock_get_redis.return_value = redis

        item = json.dumps(
            {
                "type": "unknown_type",
                "payload": {},
                "dedup_key": "dk",
                "enqueued_at": 0,
            }
        )
        shutdown = asyncio.Event()
        call_count = 0

        async def fake_brpop(key, timeout=0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (key, item)
            shutdown.set()
            return None

        redis.brpop = fake_brpop

        await _run_trigger_consumer(shutdown)

        mock_clear.assert_called_once_with("dk")


# ── Helper to match any value in assertions ───────────────────────────


def unittest_mock_any():
    """Return a value that compares equal to anything (for assertions)."""

    class _Any:
        def __eq__(self, other):
            return True

    return _Any()
