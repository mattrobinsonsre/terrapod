"""Which runs the artifact sweep is allowed to reap (#2148).

Integration tier, and necessarily so. `_cleanup_run_artifacts` decides
eligibility in a **SQL WHERE clause**, and every services-tier test of it hands
the function a list of runs through a mocked `db.execute` -- so those tests
assert what the loop does with rows it is given and can say nothing at all
about which rows it asks for. The predicate is the whole of this change, so a
mock cannot test it.

What the predicate has to get right, in both directions:

  * a PLAN-ONLY run finishes in `planned` and stays there for ever. Nothing
    supersedes it (it is not the workspace's desired state) and
    `expire_stale_plans_cycle` excludes it explicitly, so before this change
    the sweep never saw it and its artifacts outlived the deployment. On a
    production instance 18 of one workspace's 40 most recent runs were exactly
    this -- daily drift-detection plans.
  * an APPLY-CAPABLE `planned` run must survive, however old. Someone may
    still confirm it, and reaping its tfplan would break that apply. This is
    the direction that would do damage, so it is asserted first.
  * a run still executing (`pending`/`queued`/`planning`) must survive
    regardless of `plan_only`, because its own phases are still writing and
    reading these objects.
  * the retention WINDOW still applies. Nothing here reaps promptly; a
    plan-only run inside the window keeps everything.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services.artifact_retention_service import (
    RUN_ARTIFACT_KEYS,
    _cleanup_module_overrides,
    _cleanup_run_artifacts,
)

OLD = datetime.now(UTC) - timedelta(days=120)
RECENT = datetime.now(UTC) - timedelta(days=2)


class _RecordingStore:
    """Records what the sweep asked to delete. Not a mock of the DB -- the
    database here is real; only the object store is stubbed, because the
    assertion is about which keys are requested."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _seed(*runs: dict) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """One workspace plus N runs. Built through the ORM, not a hand-written
    INSERT: `runs` carries ~70 columns, so a literal INSERT breaks on the next
    NOT NULL anyone adds, with a driver error naming an unrelated column."""
    tag = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        ws = Workspace(name=f"reap-{tag}")
        db.add(ws)
        await db.flush()
        ids = []
        for fields in runs:
            created = fields.pop("created_at")
            run = Run(workspace_id=ws.id, created_by="tests", **fields)
            db.add(run)
            await db.flush()
            # `created_at` carries a server default, so it is set after the
            # insert rather than fought with in the constructor.
            await db.execute(
                text("UPDATE runs SET created_at = :c WHERE id = :i"),
                {"c": created, "i": run.id},
            )
            ids.append(run.id)
        await db.commit()
        return ws.id, ids


def _keys_for(ws_id, run_id) -> set[str]:
    return {fn(str(ws_id), str(run_id)) for fn in RUN_ARTIFACT_KEYS}


async def test_an_apply_capable_planned_run_is_never_reaped_however_old():
    """The dangerous direction. This run can still be confirmed, and its
    tfplan + lock file + plan-artifacts are what the apply phase downloads --
    so reaping them would turn a confirm into a broken apply."""
    ws_id, (run_id,) = await _seed(
        {"status": "planned", "plan_only": False, "created_at": OLD},
    )
    store = _RecordingStore()
    async with get_db_session() as db:
        await _cleanup_run_artifacts(db, store, retention_days=90, batch_size=100)

    assert not (_keys_for(ws_id, run_id) & set(store.deleted)), (
        "an apply-capable planned run had its artifacts reaped; confirming it "
        "would now fail on a missing tfplan"
    )


async def test_a_plan_only_planned_run_past_the_window_is_reaped():
    """The leak #2148 closes: this is the shape of every drift-detection run
    and every speculative plan, and nothing deleted them before."""
    ws_id, (run_id,) = await _seed(
        {"status": "planned", "plan_only": True, "created_at": OLD},
    )
    store = _RecordingStore()
    async with get_db_session() as db:
        await _cleanup_run_artifacts(db, store, retention_days=90, batch_size=100)

    missing = _keys_for(ws_id, run_id) - set(store.deleted)
    assert not missing, f"plan-only run kept these keys for ever: {sorted(missing)}"


async def test_a_plan_only_run_inside_the_window_is_untouched():
    """Nothing about this change is prompt. The retention window governs as it
    always has; only eligibility widened."""
    ws_id, (run_id,) = await _seed(
        {"status": "planned", "plan_only": True, "created_at": RECENT},
    )
    store = _RecordingStore()
    async with get_db_session() as db:
        await _cleanup_run_artifacts(db, store, retention_days=90, batch_size=100)

    assert not (_keys_for(ws_id, run_id) & set(store.deleted)), (
        "a run inside the retention window was reaped"
    )


async def test_a_still_executing_plan_only_run_is_untouched():
    """`pending`/`queued`/`planning` are live: the run's own phases are still
    writing and reading these objects. Keying on `plan_only` alone would reap
    them underneath a running Job."""
    ws_id, ids = await _seed(
        {"status": "pending", "plan_only": True, "created_at": OLD},
        {"status": "queued", "plan_only": True, "created_at": OLD},
        {"status": "planning", "plan_only": True, "created_at": OLD},
    )
    store = _RecordingStore()
    async with get_db_session() as db:
        await _cleanup_run_artifacts(db, store, retention_days=90, batch_size=100)

    for run_id in ids:
        assert not (_keys_for(ws_id, run_id) & set(store.deleted)), (
            f"a live run ({run_id}) was reaped mid-flight"
        )


async def test_a_terminal_run_is_still_reaped():
    """The pre-existing behaviour, asserted so widening the predicate cannot
    quietly narrow it."""
    ws_id, ids = await _seed(
        {"status": "applied", "plan_only": False, "created_at": OLD},
        {"status": "errored", "plan_only": False, "created_at": OLD},
        {"status": "discarded", "plan_only": False, "created_at": OLD},
        {"status": "canceled", "plan_only": False, "created_at": OLD},
    )
    store = _RecordingStore()
    async with get_db_session() as db:
        await _cleanup_run_artifacts(db, store, retention_days=90, batch_size=100)

    for run_id in ids:
        missing = _keys_for(ws_id, run_id) - set(store.deleted)
        assert not missing, f"terminal run {run_id} kept {sorted(missing)}"


# ── module override tarballs: the same predicate, and the worse case ─────────


async def test_module_override_tarballs_of_a_plan_only_run_are_reaped():
    """The module-override sweep had the identical defect, and it mattered more.

    The ONLY run that carries `module_overrides` is the module-impact run, and
    `module_impact_service` creates it `plan_only=True`. So while that sweep
    filtered on terminal states it matched almost nothing -- the one exception
    being a run the VCS poller happened to cancel on a new commit -- and these
    are real module tarballs, not small artifacts.
    """
    tag = uuid.uuid4().hex[:8]
    path = f"module_overrides/{tag}/acme/vpc/aws.tar.gz"
    await _seed(
        {
            "status": "planned",
            "plan_only": True,
            "created_at": OLD,
            "module_overrides": {"acme/vpc/aws": path},
        },
    )
    store = _RecordingStore()
    async with get_db_session() as db:
        deleted = await _cleanup_module_overrides(db, store, retention_days=14, batch_size=100)

    assert path in store.deleted, (
        "a module-impact run's override tarball was not reaped; these "
        "accumulate for ever because the run never leaves `planned`"
    )
    assert deleted == 1


async def test_module_override_tarballs_of_a_live_run_survive():
    """A run still planning needs its own override tarballs."""
    tag = uuid.uuid4().hex[:8]
    path = f"module_overrides/{tag}/acme/vpc/aws.tar.gz"
    await _seed(
        {
            "status": "planning",
            "plan_only": True,
            "created_at": OLD,
            "module_overrides": {"acme/vpc/aws": path},
        },
    )
    store = _RecordingStore()
    async with get_db_session() as db:
        await _cleanup_module_overrides(db, store, retention_days=14, batch_size=100)

    assert path not in store.deleted, "a planning run lost the overrides its plan needs"
