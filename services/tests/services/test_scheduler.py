"""Tests for the distributed task scheduler."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

from terrapod.services.scheduler import (
    AI_LANE,
    DEFAULT_AI_LANE_CONSUMERS,
    DEFAULT_LANE,
    PREFIX,
    PeriodicTaskDef,
    TriggerHandlerDef,
    _clear_dedup,
    _consumers_for_lane,
    _lane_queue_key,
    _run_periodic_loop,
    _run_queue_depth_sampler,
    _run_trigger_consumer,
    _trigger_handlers,
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


# ── Lanes (#1296) ──────────────────────────────────────────────────────
#
# The defect these cover: every triggered task shared one queue drained by a
# single consumer per replica, so deployment throughput was (replicas x 1) for
# all work at once. Independent, I/O-bound AI summaries therefore queued behind
# each other — and behind sub-second status posts — for minutes.


class TestLaneRouting:
    """Which queue an item is pushed to, and which key backs it."""

    def test_default_lane_keeps_the_original_key(self):
        # Load-bearing for rolling upgrades: an un-upgraded replica BRPOPs this
        # exact key. Suffixing it would strand every item a new replica wrote.
        assert _lane_queue_key(DEFAULT_LANE) == f"{PREFIX}:triggers"

    def test_non_default_lanes_are_suffixed(self):
        assert _lane_queue_key(AI_LANE) == f"{PREFIX}:triggers:ai"

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_enqueue_routes_to_the_handlers_lane(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        _trigger_handlers["slow_thing"] = TriggerHandlerDef("slow_thing", AsyncMock(), lane=AI_LANE)
        try:
            await enqueue_trigger("slow_thing", {"run_id": "r1"})
        finally:
            _trigger_handlers.pop("slow_thing", None)

        pushed_key = redis.lpush.await_args.args[0]
        assert pushed_key == f"{PREFIX}:triggers:ai"

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_an_unknown_type_falls_back_to_the_default_lane(self, mock_get_redis):
        # Mid-upgrade, a replica may enqueue a type this one has never
        # registered. Delayed on the shared key beats stranded on a key nobody
        # is reading.
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        await enqueue_trigger("type_from_the_future", {})
        assert redis.lpush.await_args.args[0] == f"{PREFIX}:triggers"

    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_a_default_lane_handler_still_uses_the_original_key(self, mock_get_redis):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        _trigger_handlers["quick_thing"] = TriggerHandlerDef("quick_thing", AsyncMock())
        try:
            await enqueue_trigger("quick_thing", {})
        finally:
            _trigger_handlers.pop("quick_thing", None)
        assert redis.lpush.await_args.args[0] == f"{PREFIX}:triggers"


class TestLaneConsumerCounts:
    """How much concurrency each lane gets — the actual fix for #1296."""

    def test_ai_lane_is_concurrent_by_default(self):
        with patch("terrapod.config.settings") as s:
            s.scheduler.lane_consumers = {}
            assert _consumers_for_lane(AI_LANE) == DEFAULT_AI_LANE_CONSUMERS
            assert DEFAULT_AI_LANE_CONSUMERS > 1

    def test_default_lane_stays_serial(self):
        # Deliberate: that work is sub-second, and leaving it at 1 means lanes
        # changed nothing about existing behaviour.
        with patch("terrapod.config.settings") as s:
            s.scheduler.lane_consumers = {}
            assert _consumers_for_lane(DEFAULT_LANE) == 1

    def test_operator_override_wins(self):
        with patch("terrapod.config.settings") as s:
            s.scheduler.lane_consumers = {"ai": 3}
            assert _consumers_for_lane(AI_LANE) == 3

    def test_zero_is_clamped_up_rather_than_stranding_the_lane(self):
        # A lane with a registered handler and no consumers would accept
        # enqueues forever and drain none of them — worse than any intent
        # behind setting 0.
        with patch("terrapod.config.settings") as s:
            s.scheduler.lane_consumers = {"ai": 0}
            assert _consumers_for_lane(AI_LANE) == 1

    def test_negative_is_clamped_too(self):
        with patch("terrapod.config.settings") as s:
            s.scheduler.lane_consumers = {"ai": -5}
            assert _consumers_for_lane(AI_LANE) == 1


class TestQueueWaitObservation:
    """Wait time is recorded separately from handler duration."""

    @patch("terrapod.services.scheduler.SCHEDULER_TRIGGER_WAIT")
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_wait_is_observed_from_the_enqueue_stamp(self, mock_get_redis, mock_wait):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        item = json.dumps(
            {
                "type": "waity",
                "payload": {},
                "dedup_key": None,
                "enqueued_at": time.time() - 42,
            }
        )
        redis.brpop.side_effect = [(f"{PREFIX}:triggers", item), None]

        handler = AsyncMock()
        _trigger_handlers["waity"] = TriggerHandlerDef("waity", handler)
        shutdown = asyncio.Event()

        async def stop_after_first():
            await asyncio.sleep(0.05)
            shutdown.set()

        try:
            with patch(
                "terrapod.services.scheduler.ha_role.is_leader", AsyncMock(return_value=True)
            ):
                await asyncio.gather(
                    _run_trigger_consumer(shutdown, DEFAULT_LANE), stop_after_first()
                )
        finally:
            _trigger_handlers.pop("waity", None)

        observed = mock_wait.labels.return_value.observe.call_args[0][0]
        assert 40 < observed < 45, f"expected ~42s of queue wait, got {observed}"

    @patch("terrapod.services.scheduler.SCHEDULER_TRIGGER_WAIT")
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_a_legacy_item_without_a_stamp_does_not_blow_up(self, mock_get_redis, mock_wait):
        # Items enqueued by an older replica mid-upgrade have no enqueued_at.
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        item = json.dumps({"type": "stampless", "payload": {}, "dedup_key": None})
        redis.brpop.side_effect = [(f"{PREFIX}:triggers", item), None]

        _trigger_handlers["stampless"] = TriggerHandlerDef("stampless", AsyncMock())
        shutdown = asyncio.Event()

        async def stop_after_first():
            await asyncio.sleep(0.05)
            shutdown.set()

        try:
            with patch(
                "terrapod.services.scheduler.ha_role.is_leader", AsyncMock(return_value=True)
            ):
                await asyncio.gather(
                    _run_trigger_consumer(shutdown, DEFAULT_LANE), stop_after_first()
                )
        finally:
            _trigger_handlers.pop("stampless", None)

        mock_wait.labels.return_value.observe.assert_not_called()


class TestQueueDepthSampler:
    @patch("terrapod.services.scheduler.SCHEDULER_QUEUE_DEPTH")
    @patch("terrapod.services.scheduler.get_redis_client")
    async def test_it_samples_every_lane_that_has_a_handler(self, mock_get_redis, mock_gauge):
        redis = AsyncMock()
        mock_get_redis.return_value = redis
        redis.llen.return_value = 7

        _trigger_handlers["quick"] = TriggerHandlerDef("quick", AsyncMock())
        _trigger_handlers["slow"] = TriggerHandlerDef("slow", AsyncMock(), lane=AI_LANE)
        shutdown = asyncio.Event()

        async def stop_soon():
            await asyncio.sleep(0.05)
            shutdown.set()

        try:
            await asyncio.gather(_run_queue_depth_sampler(shutdown, interval=60), stop_soon())
        finally:
            _trigger_handlers.pop("quick", None)
            _trigger_handlers.pop("slow", None)

        sampled = {c.kwargs.get("lane") for c in mock_gauge.labels.call_args_list}
        assert sampled == {DEFAULT_LANE, AI_LANE}
        mock_gauge.labels.return_value.set.assert_called_with(7)
