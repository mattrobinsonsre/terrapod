"""A local Pulumi update holds the workspace lock, and lets go of it (#1562).

The lock is the same one a Terraform CLI apply takes, so the UI shows the stack
busy and the dispatcher holds agent applies back. It is a row with no TTL, so the
tests below cover every way it is released: completion, cancellation, and the
sweep that notices an update's lease has lapsed.
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
        db = _db(_result(scalars=[gone, alive]), _result(scalar=gone))
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


class TestTheSweepIsEngineGated:
    """Registered only with the Pulumi engine on, like every Pulumi surface (#1429)."""

    def test_the_registration_sits_inside_the_engine_check(self) -> None:
        app_py = pathlib.Path(locks.__file__).resolve().parent.parent / "api" / "app.py"
        tree = ast.parse(app_py.read_text())
        guarded = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.If)
                and "engine_enabled" in ast.unparse(node.test)
                and "pulumi" in ast.unparse(node.test)
            ):
                if "pulumi_update_sweep" in ast.unparse(node):
                    guarded = True
        assert guarded, "pulumi_update_sweep must be registered only when the engine is on"
        assert app_py.read_text().count("pulumi_update_sweep") == 1
