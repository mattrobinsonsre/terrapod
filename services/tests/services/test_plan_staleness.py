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
        # #1901. Present and empty rather than absent: the production row always
        # has both columns, so a fixture that omits them would make the identity
        # guard look defensive when the honest answer is that a missing
        # attribute is a bug worth raising on.
        "oidc_audiences": {},
        "oidc_minted_targets": [],
    }
    base.update(over)
    return SimpleNamespace(**base)


def _ws(plan_expiry_seconds=None, oidc_audiences=None):
    return SimpleNamespace(
        plan_expiry_seconds=plan_expiry_seconds,
        oidc_audiences=oidc_audiences if oidc_audiences is not None else {},
    )


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


# ── #1901: _cloud_identity_moved_since_plan ──────────────────────────────────
#
# The apply must not run under a different cloud identity from the one its plan
# was reviewed under. The runner's mint path refuses this too, per target, but
# that happens inside a Job after it has been scheduled and after `init` — so
# the value here is failing before anything exists, and naming what moved.


def _settings_with(catalogue):
    return SimpleNamespace(auth=SimpleNamespace(oidc_issuer=SimpleNamespace(audiences=catalogue)))


def _identity(run, ws, catalogue):
    from unittest.mock import patch

    with patch("terrapod.config.settings", _settings_with(catalogue)):
        return run_service._cloud_identity_moved_since_plan(run, ws)


def test_identity_not_moved_when_nothing_was_minted():
    """The overwhelming majority of runs. Must not invoke the resolver at all
    as far as the outcome is concerned, and certainly must not refuse."""
    run = _run(oidc_audiences={}, oidc_minted_targets=[])
    assert _identity(run, _ws(), {"aws": ["sts.example.com"]}) is None


def test_identity_not_moved_when_the_answer_is_unchanged():
    run = _run(oidc_audiences={"aws": ["sts.example.com"]}, oidc_minted_targets=["aws"])
    assert _identity(run, _ws(), {"aws": ["sts.example.com"]}) is None


def test_identity_moved_when_a_minted_target_changed_value():
    run = _run(oidc_audiences={"aws": ["sts.example.com"]}, oidc_minted_targets=["aws"])
    reason = _identity(run, _ws(), {"aws": ["sts.other.example.com"]})
    assert reason is not None and "aws" in reason and "cloud identity" in reason


def test_identity_moved_when_a_minted_target_was_removed():
    """The apply would present no identity where the plan presented one. The
    mint would refuse it too — this just gets there first."""
    run = _run(oidc_audiences={"aws": ["sts.example.com"]}, oidc_minted_targets=["aws"])
    reason = _identity(run, _ws(), {})
    assert reason is not None and "aws" in reason


def test_identity_moved_on_a_reorder():
    """Order is what goes into `aud`. Treating a reorder as unchanged would be a
    judgement about a federation target's matching behaviour that we are in no
    position to make."""
    run = _run(oidc_audiences={"v": ["https://a", "https://b"]}, oidc_minted_targets=["v"])
    assert _identity(run, _ws(), {"v": ["https://b", "https://a"]}) is not None


def test_a_change_to_a_target_this_run_never_MINTED_for_does_not_refuse():
    """The reason the minted set is recorded at all. The snapshot is the MERGED
    map, so it carries deployment-wide catalogue entries a workspace may never
    use — scoping this to the configured set would mean one edit to the
    catalogue refusing every pending apply in the fleet."""
    run = _run(
        oidc_audiences={"aws": ["sts.example.com"], "vault": ["https://vault.example.com"]},
        oidc_minted_targets=["aws"],
    )
    assert _identity(run, _ws(), {"aws": ["sts.example.com"], "vault": ["https://moved"]}) is None


def test_a_newly_added_target_is_not_a_staleness_cause():
    """The mint reads the run's SNAPSHOT, so a target added since the plan
    yields no token at apply exactly as it yielded none at plan — the identity
    the apply presents is unchanged."""
    run = _run(oidc_audiences={"aws": ["sts.example.com"]}, oidc_minted_targets=["aws"])
    live = {"aws": ["sts.example.com"], "azurerm": ["api://exchange"]}
    assert _identity(run, _ws(), live) is None


def test_the_workspace_override_is_what_moved():
    """The merge is two-level, so a change on either side counts. Here the
    catalogue is static and the workspace's own override moved."""
    run = _run(oidc_audiences={"aws": ["sts.example.com"]}, oidc_minted_targets=["aws"])
    ws = _ws(oidc_audiences={"aws": ["sts.workspace-override.example.com"]})
    assert _identity(run, ws, {"aws": ["sts.example.com"]}) is not None


def test_identity_not_checked_without_a_workspace():
    run = _run(oidc_audiences={"aws": ["x"]}, oidc_minted_targets=["aws"])
    assert _identity(run, None, {"aws": ["y"]}) is None


@pytest.mark.asyncio
async def test_staleness_reason_reports_identity_drift_when_state_fresh():
    """Through the composite, which is what `confirm_run` calls — a test of the
    predicate alone would not prove it is reached."""
    from unittest.mock import patch

    db = AsyncMock()
    db.scalar.return_value = 5  # state unchanged
    run = _run(
        plan_state_serial=5,
        workspace_id="w",
        plan_finished_at=now_utc(),
        oidc_audiences={"aws": ["sts.example.com"]},
        oidc_minted_targets=["aws"],
    )
    with patch("terrapod.config.settings", _settings_with({"aws": ["sts.moved.example.com"]})):
        reason = await run_service._staleness_reason(db, run, _ws(3600))
    assert reason is not None and "cloud identity" in reason


@pytest.mark.asyncio
async def test_state_drift_is_still_reported_ahead_of_identity_drift():
    """Both moved. State drift is the first correctness guard and keeps
    precedence, so the message names the more fundamental cause."""
    from unittest.mock import patch

    db = AsyncMock()
    db.scalar.return_value = 9
    run = _run(
        plan_state_serial=5,
        workspace_id="w",
        plan_finished_at=now_utc(),
        oidc_audiences={"aws": ["sts.example.com"]},
        oidc_minted_targets=["aws"],
    )
    with patch("terrapod.config.settings", _settings_with({"aws": ["sts.moved.example.com"]})):
        reason = await run_service._staleness_reason(db, run, _ws(3600))
    assert reason is not None and "state changed" in reason


@pytest.mark.asyncio
async def test_identity_drift_is_reported_ahead_of_a_generic_expiry():
    """A named cause beats a timeout: "your audiences changed" tells an operator
    what to do, "expired" does not."""
    from unittest.mock import patch

    db = AsyncMock()
    db.scalar.return_value = 5
    run = _run(
        plan_state_serial=5,
        workspace_id="w",
        plan_finished_at=now_utc() - timedelta(seconds=7200),
        oidc_audiences={"aws": ["sts.example.com"]},
        oidc_minted_targets=["aws"],
    )
    with patch("terrapod.config.settings", _settings_with({"aws": ["sts.moved.example.com"]})):
        reason = await run_service._staleness_reason(db, run, _ws(3600))
    assert reason is not None and "cloud identity" in reason


@pytest.mark.asyncio
async def test_a_plan_only_run_is_never_identity_stale():
    """It never applies, so there is nothing to protect — and refusing it would
    break speculative pull-request plans on every catalogue edit."""
    from unittest.mock import patch

    db = AsyncMock()
    run = _run(plan_only=True, oidc_audiences={"aws": ["x"]}, oidc_minted_targets=["aws"])
    with patch("terrapod.config.settings", _settings_with({"aws": ["y"]})):
        assert await run_service._staleness_reason(db, run, _ws(3600)) is None


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
