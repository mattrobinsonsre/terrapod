"""Services tests for the bounded lifecycle-destroy auto-retry (catalog +
autodiscovery). Verifies the episode counter and the retry/cap/supersede gating
with a mocked DB."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services import lifecycle_destroy_retry as mod


def _run(*, source="catalog-lifecycle", status="errored", is_destroy=True, ws=None, rid=None):
    return SimpleNamespace(
        id=rid or uuid.uuid4(),
        workspace_id=ws or uuid.uuid4(),
        source=source,
        status=status,
        is_destroy=is_destroy,
    )


def _exec_returning(items):
    """A mock db.execute result whose .scalars().all() yields items."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    result.scalar_one_or_none.return_value = items[0] if items else None
    return result


def _cfg(retries=2, backoff=45):
    return SimpleNamespace(
        lifecycle_destroy_retries=retries,
        lifecycle_destroy_retry_backoff_seconds=backoff,
    )


@asynccontextmanager
async def _session_cm(db):
    yield db


# ── _consecutive_failed_destroys (episode counter) ─────────────────────


class TestConsecutiveFailedDestroys:
    async def test_counts_tail_of_errored_lifecycle_destroys(self):
        ws = uuid.uuid4()
        history = [  # newest first
            _run(ws=ws),
            _run(ws=ws),
            SimpleNamespace(  # a successful apply ends the episode
                id=uuid.uuid4(),
                workspace_id=ws,
                source="catalog",
                status="applied",
                is_destroy=False,
            ),
            _run(ws=ws),  # a prior episode — must NOT be counted
        ]
        db = MagicMock()
        db.execute = AsyncMock(return_value=_exec_returning(history))
        assert await mod._consecutive_failed_destroys(db, ws) == 2

    async def test_ignores_non_lifecycle_destroy(self):
        ws = uuid.uuid4()
        history = [_run(ws=ws, source="tfe-api")]  # a user CLI destroy
        db = MagicMock()
        db.execute = AsyncMock(return_value=_exec_returning(history))
        assert await mod._consecutive_failed_destroys(db, ws) == 0


# ── lifecycle_destroy_retry_cycle ──────────────────────────────────────


class TestRetryCycle:
    async def test_disabled_when_retries_zero(self):
        with (
            patch.object(mod, "load_runner_config", return_value=_cfg(retries=0)),
            patch.object(mod, "get_db_session") as mock_sess,
        ):
            await mod.lifecycle_destroy_retry_cycle()
        mock_sess.assert_not_called()  # short-circuits before touching the DB

    async def test_retries_eligible_errored_destroy(self):
        run = _run()
        db = MagicMock()
        db.execute = AsyncMock(return_value=_exec_returning([run]))
        db.get = AsyncMock(return_value=SimpleNamespace(id=run.workspace_id))
        db.commit = AsyncMock()
        cv = SimpleNamespace(id=uuid.uuid4())
        with (
            patch.object(mod, "load_runner_config", return_value=_cfg(retries=2)),
            patch.object(mod, "get_db_session", return_value=_session_cm(db)),
            patch.object(mod, "_latest_run", AsyncMock(return_value=run)),
            patch.object(mod, "_consecutive_failed_destroys", AsyncMock(return_value=1)),
            patch.object(mod.run_service, "get_latest_uploaded_cv", AsyncMock(return_value=cv)),
            patch.object(mod.run_service, "create_run", AsyncMock(return_value=MagicMock())) as mk,
            patch.object(mod.run_service, "queue_run", AsyncMock()) as qk,
        ):
            await mod.lifecycle_destroy_retry_cycle()
        mk.assert_awaited_once()
        assert mk.await_args.kwargs["is_destroy"] is True
        assert mk.await_args.kwargs["auto_apply"] is True
        assert mk.await_args.kwargs["source"] == "catalog-lifecycle"
        qk.assert_awaited_once()
        db.commit.assert_awaited_once()

    async def test_no_retry_when_cap_exhausted(self):
        run = _run()
        db = MagicMock()
        db.execute = AsyncMock(return_value=_exec_returning([run]))
        with (
            patch.object(mod, "load_runner_config", return_value=_cfg(retries=2)),
            patch.object(mod, "get_db_session", return_value=_session_cm(db)),
            patch.object(mod, "_latest_run", AsyncMock(return_value=run)),
            patch.object(mod, "_consecutive_failed_destroys", AsyncMock(return_value=3)),  # = cap+1
            patch.object(mod.run_service, "create_run", AsyncMock()) as mk,
        ):
            await mod.lifecycle_destroy_retry_cycle()
        mk.assert_not_called()

    async def test_no_retry_when_superseded(self):
        run = _run()
        newer = _run(ws=run.workspace_id)  # a different, newer latest run
        db = MagicMock()
        db.execute = AsyncMock(return_value=_exec_returning([run]))
        with (
            patch.object(mod, "load_runner_config", return_value=_cfg(retries=2)),
            patch.object(mod, "get_db_session", return_value=_session_cm(db)),
            patch.object(mod, "_latest_run", AsyncMock(return_value=newer)),
            patch.object(mod, "_consecutive_failed_destroys", AsyncMock(return_value=1)),
            patch.object(mod.run_service, "create_run", AsyncMock()) as mk,
        ):
            await mod.lifecycle_destroy_retry_cycle()
        mk.assert_not_called()
