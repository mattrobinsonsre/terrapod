"""A local Pulumi update holds the workspace lock, and lets go of it (#1562).

The lock is the same one a Terraform CLI apply takes, so the UI shows the stack
busy and the dispatcher holds agent applies back. It is a row with no TTL, so the
tests below cover every way it is released: completion, cancellation, the sweep
that notices an update's lease has lapsed, and — for an agent run, where Terrapod
knows the Job is gone rather than inferring it — the run ending (#1882).
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql

from terrapod.services import pulumi_update_locks as locks

pytestmark = pytest.mark.asyncio

WS = uuid.uuid4()


def _result(*, first=None, scalar=None, scalars=None) -> MagicMock:
    r = MagicMock()
    r.first.return_value = first
    r.scalar_one_or_none.return_value = scalar
    r.scalars.return_value.all.return_value = scalars or []
    # The CLI-run lookup the sweep makes reads `.scalars().first()` (#1563).
    r.scalars.return_value.first.return_value = scalars[0] if scalars else None
    return r


def _db(*results) -> AsyncMock:
    db = AsyncMock()
    db.execute.side_effect = list(results)
    return db


@pytest.fixture(autouse=True)
def _no_events():
    with patch("terrapod.redis.client.publish_workspace_event", AsyncMock()) as publish:
        yield publish


def _ws(*, locked=False, lock_id=None) -> SimpleNamespace:
    return SimpleNamespace(id=WS, locked=locked, lock_id=lock_id)


class TestTakingTheLock:
    async def test_an_idle_workspace_is_locked_in_the_updates_name(self, _no_events) -> None:
        ws = _ws()
        db = _db(_result(first=None), _result(scalar=ws))
        await locks.take_workspace_lock(db, WS, "u-1")
        assert ws.locked is True
        assert ws.lock_id == "pulumi-update:u-1"
        db.commit.assert_awaited_once()
        _no_events.assert_awaited_once()

    async def test_a_previous_locks_reason_is_not_reported_as_this_ones(self) -> None:
        """#1705: the reason and holder describe the current lock only."""
        ws = SimpleNamespace(
            id=WS, locked=False, lock_id=None, lock_reason="old note", locked_by="a@example.test"
        )
        db = _db(_result(first=None), _result(scalar=ws))
        await locks.take_workspace_lock(db, WS, "u-1")
        assert ws.lock_reason == "pulumi update"
        assert ws.locked_by is None

    async def test_an_agent_apply_in_flight_refuses_it(self) -> None:
        db = _db(_result(first=(uuid.uuid4(),)))
        with pytest.raises(locks.LockRefused, match="agent run is applying"):
            await locks.take_workspace_lock(db, WS, "u-1")
        # Never even tried to take the lock.
        assert db.execute.await_count == 1

    async def test_a_locked_workspace_refuses_it_and_names_the_holder(self) -> None:
        ws = _ws(locked=True, lock_id="lock-alice@example.test")
        db = _db(_result(first=None), _result(scalar=ws))
        with pytest.raises(locks.LockRefused) as exc:
            await locks.take_workspace_lock(db, WS, "u-1")
        assert "lock-alice@example.test" in exc.value.message
        assert ws.lock_id == "lock-alice@example.test"
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()

    async def test_the_row_is_read_for_update(self) -> None:
        """Read FOR UPDATE, so a Terraform CLI lock arriving at the same moment
        waits for this decision instead of also winning."""
        db = _db(_result(first=None), _result(scalar=_ws()))
        await locks.take_workspace_lock(db, WS, "u-1")
        sql = str(db.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE" in sql

    async def test_a_missing_workspace_refuses_it(self) -> None:
        db = _db(_result(first=None), _result(scalar=None))
        with pytest.raises(locks.LockRefused, match="no longer exists"):
            await locks.take_workspace_lock(db, WS, "u-1")
        db.commit.assert_not_awaited()


class TestReleasingIt:
    async def test_this_updates_lock_is_released(self, _no_events) -> None:
        ws = _ws(locked=True, lock_id="pulumi-update:u-1")
        db = _db(_result(scalar=ws))
        assert await locks.release_workspace_lock(db, WS, "u-1") is True
        assert ws.locked is False
        assert ws.lock_id is None
        assert ws.lock_reason is None
        assert ws.locked_by is None
        db.commit.assert_awaited_once()
        _no_events.assert_awaited_once()

    async def test_a_lock_that_is_not_this_updates_is_left_alone(self, _no_events) -> None:
        """Force-unlocked since, or taken by something else: not ours to release."""
        ws = _ws(locked=True, lock_id="lock-alice@example.test")
        db = _db(_result(scalar=ws))
        assert await locks.release_workspace_lock(db, WS, "u-1") is False
        assert ws.locked is True
        assert ws.lock_id == "lock-alice@example.test"
        db.commit.assert_not_awaited()
        _no_events.assert_not_awaited()

    async def test_an_unlocked_workspace_is_left_alone(self, _no_events) -> None:
        ws = _ws()
        db = _db(_result(scalar=ws))
        assert await locks.release_workspace_lock(db, WS, "u-1") is False
        db.commit.assert_not_awaited()


def _session(db):
    @asynccontextmanager
    async def session():
        yield db

    return session


class TestTheSweep:
    PROMOTE = "terrapod.services.pulumi_checkpoint_service.promote_checkpoint"

    def _redis(self, *alive: str) -> AsyncMock:
        redis = AsyncMock()
        redis.exists.side_effect = lambda key: any(key.endswith(f":{a}") for a in alive)
        redis.get.return_value = b"gone"
        return redis

    async def _sweep(self, db, redis, promote) -> int:
        with (
            patch("terrapod.db.session.get_db_session", _session(db)),
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(self.PROMOTE, promote),
        ):
            return await locks.sweep_abandoned_updates()

    async def test_an_abandoned_update_is_stored_and_then_released(self) -> None:
        """Its last checkpoint is the only record of what it created (#1564)."""
        gone = _ws(locked=True, lock_id="pulumi-update:gone")
        alive = SimpleNamespace(id=uuid.uuid4(), locked=True, lock_id="pulumi-update:alive")
        # Third result: the sweep now also looks for a CLI run to end (#1563).
        # None here — this abandoned update has no run recorded against it.
        db = _db(_result(scalars=[gone, alive]), _result(scalar=gone), _result())
        redis = self._redis("alive")
        promote = AsyncMock()
        assert await self._sweep(db, redis, promote) == 1
        promote.assert_awaited_once_with(db, gone, "gone")
        assert gone.locked is False
        assert alive.locked is True
        # The stack mutex it still held is cleared with it.
        redis.delete.assert_awaited_once_with(f"tp:pulumi:stack_active:{WS}")

    async def test_a_live_update_is_left_alone(self) -> None:
        alive = _ws(locked=True, lock_id="pulumi-update:alive")
        db = _db(_result(scalars=[alive]))
        promote = AsyncMock()
        assert await self._sweep(db, self._redis("alive"), promote) == 0
        promote.assert_not_awaited()
        assert alive.locked is True
        assert db.execute.await_count == 1

    async def test_a_failed_promotion_keeps_the_lock_for_the_next_cycle(self) -> None:
        """Releasing first would leave the checkpoint held against an update
        nothing looks at again."""
        gone = _ws(locked=True, lock_id="pulumi-update:gone")
        db = _db(_result(scalars=[gone]))
        promote = AsyncMock(side_effect=RuntimeError("storage down"))
        assert await self._sweep(db, self._redis(), promote) == 0
        assert gone.locked is True
        assert gone.lock_id == "pulumi-update:gone"
        db.rollback.assert_awaited_once()

    async def test_it_only_looks_at_pulumi_locks(self) -> None:
        """A Terraform CLI lock or a manual one is never the sweep's to release."""
        db = _db(_result(scalars=[]))
        await self._sweep(db, self._redis(), AsyncMock())
        compiled = db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True})
        assert "LIKE 'pulumi-update:%'" in str(compiled)


class TestTheSweepIsRegisteredUnconditionally:
    """It used to be registered only with the Pulumi engine on (#1429).

    That switch is withdrawn (#1986), so the inverse is what needs guarding: a
    conditional registration is how a lapsed lease ends up with nothing to
    notice it, holding the workspace lock and keeping the next apply back.
    """

    def test_it_is_registered_exactly_once_and_under_no_condition(self) -> None:
        app_py = pathlib.Path(locks.__file__).resolve().parent.parent / "api" / "app.py"
        source = app_py.read_text()
        tree = ast.parse(source)

        assert source.count("pulumi_update_sweep") == 1, (
            "registered more than once — the second registration would compete "
            "with the first for the same Redis claim"
        )
        inside_a_conditional = [
            ast.unparse(node.test)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "pulumi_update_sweep" in ast.unparse(node)
        ]
        assert not inside_a_conditional, (
            "the sweep's registration is behind a condition: "
            f"{inside_a_conditional} — there is no engine switch any more (#1986), "
            "and a sweep that does not run leaves a lapsed lease holding the lock"
        )


RUN = uuid.uuid4()
UPDATE = "u-run"


def _record(**over: str) -> dict[bytes, bytes]:
    base = {"workspace_id": str(WS), "kind": "update", "run_id": str(RUN)}
    base.update(over)
    return {k.encode(): v.encode() for k, v in base.items()}


class TestEndingAnAgentRunsUpdate:
    """The run is over, so Terrapod ends its update rather than waiting for the
    sweep to infer it from a lapsed lease (#1882)."""

    PROMOTE = "terrapod.services.pulumi_checkpoint_service.promote_checkpoint"

    def _redis(self, *, mutex: str | None = UPDATE, record=None, mutex_after=...) -> AsyncMock:
        """A Redis holding one stack mutex and one update record.

        `mutex_after` is what the mutex re-read before the delete returns; it
        defaults to the same update, and is set to something else to stand for a
        newer update having taken the stack in the meantime.
        """
        redis = AsyncMock()
        after = mutex if mutex_after is ... else mutex_after
        reads = [
            None if mutex is None else mutex.encode(),
            None if after is None else after.encode(),
        ]
        redis.get.side_effect = reads
        redis.hgetall.return_value = _record() if record is None else record
        return redis

    async def _handle(self, db, redis, promote, *, run_id: str = str(RUN)) -> None:
        with (
            patch("terrapod.db.session.get_db_session", _session(db)),
            patch("terrapod.redis.client.get_redis_client", return_value=redis),
            patch(self.PROMOTE, promote),
        ):
            await locks.handle_run_ended({"run_id": run_id, "workspace_id": str(WS)})

    async def test_the_runs_update_is_promoted_and_the_stack_let_go(self) -> None:
        ws = _ws(locked=True, lock_id=f"pulumi-update:{UPDATE}")
        db = _db(_result(scalar=ws), _result(scalar=ws))
        redis = self._redis()
        promote = AsyncMock()
        await self._handle(db, redis, promote)

        promote.assert_awaited_once_with(db, ws, UPDATE)
        assert ws.locked is False
        assert ws.lock_id is None
        redis.delete.assert_any_await(f"tp:pulumi:update:{UPDATE}")
        redis.delete.assert_any_await(f"tp:pulumi:stack_active:{WS}")

    async def test_the_checkpoint_is_promoted_before_anything_is_released(self) -> None:
        """The sweep's rule, for the sweep's reason: releasing first would leave
        the checkpoint held against an update nothing looks at again. It is also
        what makes this safe to race the sweep — while the record still exists
        the sweep skips this update entirely."""
        order: list[str] = []
        ws = _ws(locked=True, lock_id=f"pulumi-update:{UPDATE}")

        db = _db(_result(scalar=ws), _result(scalar=ws))
        db.commit.side_effect = lambda: order.append("release-committed")
        redis = self._redis()
        redis.delete.side_effect = lambda key: order.append(f"deleted:{key.split(':')[2]}")
        promote = AsyncMock(side_effect=lambda *a: order.append("promoted"))

        await self._handle(db, redis, promote)

        assert order[0] == "promoted", order
        assert set(order[1:]) == {"deleted:update", "deleted:stack_active", "release-committed"}

    async def test_a_local_clis_update_is_left_alone(self) -> None:
        """A person's `pulumi up` may hold this stack: it is refused only while
        an agent run is applying, so one that began before that is legitimate."""
        ws = _ws(locked=True, lock_id=f"pulumi-update:{UPDATE}")
        db = _db(_result(scalar=ws))
        record = _record()
        del record[b"run_id"]
        redis = self._redis(record=record)
        promote = AsyncMock()

        await self._handle(db, redis, promote)

        promote.assert_not_awaited()
        redis.delete.assert_not_awaited()
        assert ws.locked is True
        db.execute.assert_not_awaited()

    async def test_another_runs_update_is_left_alone(self) -> None:
        ws = _ws(locked=True, lock_id=f"pulumi-update:{UPDATE}")
        db = _db(_result(scalar=ws))
        redis = self._redis(record=_record(run_id=str(uuid.uuid4())))
        promote = AsyncMock()

        await self._handle(db, redis, promote)

        promote.assert_not_awaited()
        redis.delete.assert_not_awaited()
        assert ws.locked is True

    async def test_a_lapsed_record_is_left_to_the_sweep(self) -> None:
        """Without the record there is no identity to claim the update by, and
        the sweep is exactly the thing that handles a lapsed lease."""
        db = _db()
        redis = self._redis(record={})
        promote = AsyncMock()

        await self._handle(db, redis, promote)

        promote.assert_not_awaited()
        redis.delete.assert_not_awaited()

    async def test_a_stack_with_nothing_in_flight_does_nothing(self) -> None:
        """Also the preview case: a preview takes neither mutex nor lock, so it
        is found by neither and its inert record expires on its own."""
        db = _db()
        redis = self._redis(mutex=None)
        promote = AsyncMock()

        await self._handle(db, redis, promote)

        promote.assert_not_awaited()
        redis.hgetall.assert_not_awaited()
        redis.delete.assert_not_awaited()

    async def test_a_failed_promotion_releases_nothing(self) -> None:
        ws = _ws(locked=True, lock_id=f"pulumi-update:{UPDATE}")
        db = _db(_result(scalar=ws))
        redis = self._redis()
        promote = AsyncMock(side_effect=RuntimeError("storage down"))

        await self._handle(db, redis, promote)

        redis.delete.assert_not_awaited()
        assert ws.locked is True
        assert ws.lock_id == f"pulumi-update:{UPDATE}"
        db.rollback.assert_awaited_once()

    async def test_a_mutex_a_newer_update_has_taken_is_not_deleted(self) -> None:
        """The same guard `complete_update` applies: deleting a newer update's
        mutex would let a third start alongside it."""
        ws = _ws(locked=True, lock_id=f"pulumi-update:{UPDATE}")
        db = _db(_result(scalar=ws), _result(scalar=ws))
        redis = self._redis(mutex_after="u-newer")

        await self._handle(db, redis, AsyncMock())

        redis.delete.assert_awaited_once_with(f"tp:pulumi:update:{UPDATE}")

    async def test_the_workspace_lock_goes_through_the_conditional_release(self) -> None:
        """A lock the update no longer holds is not this run's to clear."""
        ws = _ws(locked=True, lock_id="lock-alice@example.test")
        db = _db(_result(scalar=ws), _result(scalar=ws))

        await self._handle(db, self._redis(), AsyncMock())

        assert ws.locked is True
        assert ws.lock_id == "lock-alice@example.test"

    async def test_a_missing_workspace_does_nothing(self) -> None:
        db = _db(_result(scalar=None))
        redis = self._redis()
        await self._handle(db, redis, AsyncMock())
        redis.delete.assert_not_awaited()

    async def test_a_payload_naming_no_run_does_nothing(self) -> None:
        redis = AsyncMock()
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await locks.handle_run_ended({"workspace_id": str(WS)})
            await locks.handle_run_ended({"run_id": str(RUN)})
        redis.get.assert_not_awaited()

    async def test_it_reads_the_workspace_by_id(self) -> None:
        """The statement, not just the result: a query narrowed or pointed at
        another table would otherwise keep passing against a fake."""
        ws = _ws(locked=True, lock_id=f"pulumi-update:{UPDATE}")
        db = _db(_result(scalar=ws), _result(scalar=ws))
        await self._handle(db, self._redis(), AsyncMock())
        sql = str(
            db.execute.await_args_list[0]
            .args[0]
            .compile(compile_kwargs={"literal_binds": True}, dialect=postgresql.dialect())
        )
        assert "FROM workspaces" in sql
        assert f"workspaces.id = '{WS}'" in sql


class TestAskingForItWhenARunEnds:
    """`transition_run` is the one place every terminal path funnels through, so
    it is where the request is raised — as a trigger, because promoting and
    releasing both commit and the not-ours path rolls back, none of which may
    happen on the transition's own session."""

    TRIGGER = "terrapod.services.scheduler.enqueue_trigger"

    def _run(self, *, plan_only: bool = False) -> SimpleNamespace:
        return SimpleNamespace(id=RUN, workspace_id=WS, plan_only=plan_only)

    def _db_for(self, engine: str | None) -> AsyncMock:
        db = AsyncMock()
        db.get.return_value = None if engine is None else SimpleNamespace(id=WS, engine=engine)
        return db

    async def _enqueue(self, db, run, *, enqueue=None) -> AsyncMock:
        from terrapod.services import run_service

        enqueue = enqueue or AsyncMock()
        with patch(self.TRIGGER, enqueue):
            await run_service._enqueue_pulumi_run_ended(db, run)
        return enqueue

    async def test_a_terminal_pulumi_run_asks_for_its_update_to_be_ended(self) -> None:
        enqueue = await self._enqueue(self._db_for("pulumi"), self._run())
        enqueue.assert_awaited_once()
        assert enqueue.await_args.args[0] == locks.RUN_ENDED_TRIGGER
        assert enqueue.await_args.args[1] == {"run_id": str(RUN), "workspace_id": str(WS)}

    async def test_a_terraform_run_is_untouched(self) -> None:
        """With the engine on as well: Pulumi's arrival costs Terraform nothing."""
        enqueue = await self._enqueue(self._db_for("terraform"), self._run())
        enqueue.assert_not_awaited()

    async def test_a_plan_only_run_is_untouched(self) -> None:
        """A preview takes neither the stack mutex nor the workspace lock, and a
        plan-only run is granted no capability to begin anything else."""
        db = self._db_for("pulumi")
        enqueue = await self._enqueue(db, self._run(plan_only=True))
        enqueue.assert_not_awaited()
        db.get.assert_not_awaited()

    async def test_a_failed_enqueue_never_breaks_the_transition(self) -> None:
        """Best-effort: the sweep is still the backstop."""
        broken = AsyncMock(side_effect=RuntimeError("redis down"))
        await self._enqueue(self._db_for("pulumi"), self._run(), enqueue=broken)

    async def test_it_is_raised_for_every_terminal_state(self) -> None:
        """Cancelled, OOM-killed, preempted, errored by the reconciler — and
        applied, where a CLI that died after its last checkpoint leaves the same
        residue. Read off `transition_run` itself so a narrower condition (say
        `errored` only) fails here."""
        from terrapod.services import run_service

        src = pathlib.Path(run_service.__file__).read_text()
        tree = ast.parse(src)
        called_under = [
            ast.unparse(node.test)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "_enqueue_pulumi_run_ended" in ast.unparse(node)
        ]
        assert any("TERMINAL_STATES" in test for test in called_under), called_under


class TestTheRunEndedHandlerIsRegisteredUnconditionally:
    """It used to be registered only with the Pulumi engine on (#1429).

    Withdrawn with the switch (#1986). The property that replaces it: a handler
    behind a condition means `_enqueue_pulumi_run_ended` pushes items into a
    queue nothing drains, which is silent — the enqueue succeeds and the update
    is never ended.
    """

    def test_it_is_registered_under_no_condition(self) -> None:
        app_py = pathlib.Path(locks.__file__).resolve().parent.parent / "api" / "app.py"
        source = app_py.read_text()
        tree = ast.parse(source)

        total = source.count("handle_run_ended")
        assert total, "handle_run_ended is never registered"
        conditional = [
            ast.unparse(node.test)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "handle_run_ended" in ast.unparse(node)
        ]
        assert not conditional, (
            f"handle_run_ended sits behind {conditional} — an unregistered "
            "handler leaves enqueued items in a queue nothing drains"
        )
