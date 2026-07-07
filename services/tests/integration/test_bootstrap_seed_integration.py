"""Integration tests for the eval-only sample-workspace bootstrap seed (#707).

The seed inserts a workspace + configuration version + a terminal plan-only run
directly as DB rows so an evaluation instance shows a populated UI on first
login. These tests use a real Postgres (the seed is row-creation + idempotency
logic — exactly Postgres semantics), asserting the rows land and that re-running
the seed is a no-op.
"""

from sqlalchemy import func, select

from terrapod.cli.bootstrap import _bootstrap_sample_workspace
from terrapod.db.models import ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db_session


async def _seed(pool_name: str = "", owner: str = "admin@test.com") -> None:
    async with get_db_session() as session:
        async with session.begin():
            await _bootstrap_sample_workspace(session, pool_name, owner)


async def test_seed_creates_workspace_cv_and_planned_run(app):
    await _seed()

    async with get_db_session() as session:
        ws = (
            await session.execute(select(Workspace).where(Workspace.name == "example-vpc"))
        ).scalar_one()
        assert ws.execution_mode == "agent"
        assert ws.owner_email == "admin@test.com"
        # Labels are visible in the UI — part of what makes the first screen look real.
        assert ws.labels == {"env": "demo", "team": "platform"}
        # Reserved-label keys must never be seeded (env/team are not reserved).
        assert "status" not in ws.labels and "pool" not in ws.labels

        cvs = (
            (
                await session.execute(
                    select(ConfigurationVersion).where(ConfigurationVersion.workspace_id == ws.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(cvs) == 1
        assert cvs[0].status == "uploaded"
        # Must not auto-queue a real run — the seeded run is synthetic.
        assert cvs[0].auto_queue_runs is False

        runs = (await session.execute(select(Run).where(Run.workspace_id == ws.id))).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        # Terminal, plan-only, with a change summary the UI renders as a badge.
        assert run.status == "planned"
        assert run.plan_only is True
        assert run.has_changes is True
        assert run.resource_additions == 3
        assert run.plan_finished_at is not None


async def test_seed_is_idempotent(app):
    await _seed()
    await _seed()  # second run must be a no-op, not a duplicate / IntegrityError

    async with get_db_session() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(Workspace).where(Workspace.name == "example-vpc")
            )
        ).scalar_one()
        assert count == 1
