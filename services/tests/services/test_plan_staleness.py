"""Unit tests for the plan-staleness guard predicates (#646 expiry, #647 state drift).

These cover the pure decision logic in run_service that decides whether an
apply-capable planned run may still be applied. The multi-row lifecycle (a new
state version auto-discarding stale plans, the TTL sweep) is exercised against a
real database in tests/integration/test_run_execution.py.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from terrapod.db.models import now_utc
from terrapod.services import run_service


def _run(**over):
    base = {
        "plan_only": False,
        "is_drift_detection": False,
        "vcs_pull_request_number": None,
        "plan_state_serial": None,
        "plan_finished_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _ws(plan_expiry_seconds=None):
    return SimpleNamespace(plan_expiry_seconds=plan_expiry_seconds)


# ── #646: _plan_expired (pure) ───────────────────────────────────────────────


def test_plan_expired_disabled_when_ttl_unset_or_zero():
    finished = now_utc() - timedelta(hours=10)
    assert run_service._plan_expired(_run(plan_finished_at=finished), _ws(None)) is False
    assert run_service._plan_expired(_run(plan_finished_at=finished), _ws(0)) is False


def test_plan_expired_false_without_plan_finished_at():
    assert run_service._plan_expired(_run(plan_finished_at=None), _ws(3600)) is False


def test_plan_expired_false_for_plan_only():
    finished = now_utc() - timedelta(hours=10)
    assert (
        run_service._plan_expired(_run(plan_only=True, plan_finished_at=finished), _ws(3600))
        is False
    )


def test_plan_expired_true_when_aged_past_ttl():
    finished = now_utc() - timedelta(seconds=7200)
    assert run_service._plan_expired(_run(plan_finished_at=finished), _ws(3600)) is True


def test_plan_expired_false_within_ttl():
    finished = now_utc() - timedelta(seconds=100)
    assert run_service._plan_expired(_run(plan_finished_at=finished), _ws(3600)) is False


# ── #647: _state_moved_since_plan (needs current serial from db.scalar) ───────


@pytest.mark.asyncio
async def test_state_not_stale_without_baseline():
    # No snapshot (first apply) → never stale; db not even consulted.
    db = AsyncMock()
    assert await run_service._state_moved_since_plan(db, _run(plan_state_serial=None)) is None
    db.scalar.assert_not_called()


@pytest.mark.asyncio
async def test_state_not_stale_when_serial_unchanged():
    db = AsyncMock()
    db.scalar.return_value = 5
    run = _run(plan_state_serial=5, workspace_id="w")
    assert await run_service._state_moved_since_plan(db, run) is None


@pytest.mark.asyncio
async def test_state_stale_when_serial_advanced():
    db = AsyncMock()
    db.scalar.return_value = 7
    run = _run(plan_state_serial=5, workspace_id="w")
    assert await run_service._state_moved_since_plan(db, run) == 7


# ── _staleness_reason (combines both; plan-only never stale) ─────────────────


@pytest.mark.asyncio
async def test_staleness_reason_none_for_plan_only():
    db = AsyncMock()
    run = _run(plan_only=True, plan_state_serial=5)
    assert await run_service._staleness_reason(db, run, _ws(1)) is None


@pytest.mark.asyncio
async def test_staleness_reason_reports_state_change_first():
    db = AsyncMock()
    db.scalar.return_value = 9
    run = _run(plan_state_serial=5, workspace_id="w", plan_finished_at=now_utc())
    reason = await run_service._staleness_reason(db, run, _ws(3600))
    assert reason is not None and "state changed" in reason and "5 -> 9" in reason


@pytest.mark.asyncio
async def test_staleness_reason_reports_expiry_when_state_fresh():
    db = AsyncMock()
    db.scalar.return_value = 5  # unchanged
    run = _run(
        plan_state_serial=5,
        workspace_id="w",
        plan_finished_at=now_utc() - timedelta(seconds=7200),
    )
    reason = await run_service._staleness_reason(db, run, _ws(3600))
    assert reason is not None and "plan expired after 3600s" == reason


@pytest.mark.asyncio
async def test_staleness_reason_none_when_fresh():
    db = AsyncMock()
    db.scalar.return_value = 5
    run = _run(plan_state_serial=5, workspace_id="w", plan_finished_at=now_utc())
    assert await run_service._staleness_reason(db, run, _ws(None)) is None


class TestStateVersionSitesInvalidateStalePlans:
    """Source-introspection invariant (#647): EVERY API router that constructs a
    new StateVersion (which bumps the workspace's serial) MUST also call
    `discard_stale_plans_for_state_change`, so a state change from any path —
    CLI state-version create, runner post-apply, rollback, manual upload — kills
    stale planned runs. A future creation site added without the hook fails here
    loudly rather than silently letting a stale plan apply outdated config.
    """

    def test_every_state_version_creation_site_calls_the_discard_hook(self):
        import pathlib

        routers_dir = pathlib.Path(run_service.__file__).parent.parent / "api" / "routers"
        offenders = []
        for path in sorted(routers_dir.glob("*.py")):
            src = path.read_text()
            constructs_sv = "StateVersion(" in src
            calls_hook = "discard_stale_plans_for_state_change" in src
            if constructs_sv and not calls_hook:
                offenders.append(path.name)
        assert offenders == [], (
            "router(s) create a StateVersion without invalidating stale plans "
            f"via discard_stale_plans_for_state_change: {offenders}"
        )


class TestDiscardHookIsBestEffort:
    """The state-version discard hook must never propagate — it runs inside a
    state-version write transaction, and a state write must not be lost because
    a stale-plan cleanup failed on one run (#665)."""

    async def test_discard_failure_does_not_propagate_and_continues(self):
        import uuid
        from unittest.mock import MagicMock, patch

        run_a = SimpleNamespace(id=uuid.uuid4(), status="planned", plan_state_serial=1)
        run_b = SimpleNamespace(id=uuid.uuid4(), status="planned", plan_state_serial=1)

        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [run_a, run_b]
        db.execute.return_value = result

        calls = []

        async def _boom(db_, run, reason):
            calls.append(run)
            if run is run_a:
                raise RuntimeError("transient discard failure")

        with patch.object(run_service, "discard_run", new=_boom):
            # Must NOT raise even though run_a's discard blows up.
            discarded = await run_service.discard_stale_plans_for_state_change(db, uuid.uuid4(), 2)

        assert calls == [run_a, run_b]  # kept going past the failure
        assert discarded == 1  # only the successful discard counted
