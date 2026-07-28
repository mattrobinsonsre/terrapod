"""Integration: multi-pool run dispatch against real Postgres (#1085).

The property that matters is **exactly-once execution**. A workspace can name
several agent pools and every one of them is offered the run, so the obvious
worry is "does offering it to N pools produce N claims?" It cannot — the run is
a single row claimed under ``SELECT … FOR UPDATE SKIP LOCKED`` — but that is a
Postgres-row-level guarantee, which mocked tests cannot demonstrate. Hence a
real engine.
"""

import asyncio

import pytest
from sqlalchemy import select

from terrapod.db.models import AgentPool, ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import pool_set, run_service

pytestmark = pytest.mark.integration


async def _seed(pool_names: list[str]) -> tuple[Workspace, list[AgentPool]]:
    """Create a workspace whose pool set is `pool_names`, plus an uploaded CV."""
    async with get_db_session() as db:
        pools = [AgentPool(name=n) for n in pool_names]
        for p in pools:
            db.add(p)
        await db.flush()

        ws = Workspace(
            name=f"multi-pool-{pool_names[0]}",
            execution_mode="agent",
        )
        pool_set.set_workspace_pools(ws, [p.id for p in pools])
        db.add(ws)
        await db.flush()

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        return ws, pools


async def _queue_run(ws_id, cv_id, **kwargs) -> Run:
    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
        run = await run_service.create_run(db, ws, configuration_version_id=cv_id, **kwargs)
        run = await run_service.transition_run(db, run, "queued")
        await db.commit()
        return run


async def _latest_cv(ws_id):
    async with get_db_session() as db:
        return (
            await db.execute(
                select(ConfigurationVersion).where(ConfigurationVersion.workspace_id == ws_id)
            )
        ).scalar_one()


class TestMultiPoolClaim:
    async def test_run_snapshots_the_whole_pool_set(self, app):
        ws, pools = await _seed(["snap-a", "snap-b", "snap-c"])
        cv = await _latest_cv(ws.id)
        run = await _queue_run(ws.id, cv.id)

        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
            assert stored.pool_id == pools[0].id
            assert stored.pool_extra_ids == [str(pools[1].id), str(pools[2].id)]

    async def test_a_pool_that_is_not_element_zero_can_claim(self, app):
        ws, pools = await _seed(["claim-a", "claim-b"])
        cv = await _latest_cv(ws.id)
        run = await _queue_run(ws.id, cv.id)

        async with get_db_session() as db:
            import uuid as _uuid

            claim = await run_service.claim_next_run(db, _uuid.uuid4(), pools[1].id, "listener-b")
            await db.commit()

        assert claim is not None
        claimed, phase = claim
        assert phase == "plan"
        assert claimed.id == run.id
        # The run now names the pool executing it.
        assert claimed.pool_id == pools[1].id

    async def test_concurrent_claims_from_both_pools_yield_exactly_one(self, app):
        """The whole point: offering a run to N pools must not run it N times.

        Two listeners in different pools race for the same run. SKIP LOCKED
        means exactly one transaction sees a claimable row; the other gets
        nothing rather than blocking or double-claiming.
        """
        import uuid as _uuid

        ws, pools = await _seed(["race-a", "race-b"])
        cv = await _latest_cv(ws.id)
        await _queue_run(ws.id, cv.id)

        async def _try_claim(pool_id):
            async with get_db_session() as db:
                claim = await run_service.claim_next_run(db, _uuid.uuid4(), pool_id, "racer")
                await db.commit()
                return claim

        results = await asyncio.gather(
            _try_claim(pools[0].id),
            _try_claim(pools[1].id),
            return_exceptions=True,
        )
        claims = [r for r in results if isinstance(r, tuple)]
        assert len(claims) == 1, f"expected exactly one claim, got {results}"

        # And the run is in-flight exactly once.
        async with get_db_session() as db:
            runs = list((await db.execute(select(Run).where(Run.workspace_id == ws.id))).scalars())
            assert len(runs) == 1
            assert runs[0].status == "planning"

    async def test_single_pool_workspace_is_unchanged(self, app):
        """Back-compat: one pool behaves exactly as it did before #1085."""
        import uuid as _uuid

        ws, pools = await _seed(["solo"])
        cv = await _latest_cv(ws.id)
        run = await _queue_run(ws.id, cv.id)

        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
            assert stored.pool_id == pools[0].id
            assert stored.pool_extra_ids == []

            claim = await run_service.claim_next_run(db, _uuid.uuid4(), pools[0].id, "listener-1")
            await db.commit()
        assert claim is not None
        assert claim[0].pool_id == pools[0].id

    async def test_a_pool_outside_the_set_claims_nothing(self, app):
        import uuid as _uuid

        ws, _pools = await _seed(["scoped-a", "scoped-b"])
        cv = await _latest_cv(ws.id)
        await _queue_run(ws.id, cv.id)

        async with get_db_session() as db:
            stranger = AgentPool(name="unrelated")
            db.add(stranger)
            await db.flush()
            claim = await run_service.claim_next_run(db, _uuid.uuid4(), stranger.id, "outsider")
            await db.commit()
        assert claim is None


class TestPoolDeletionCascade:
    """Deleting a pool detaches it from every workspace (#1087).

    This is the integrity the mapping table exists for: `delete_pool` is a bare
    DELETE that relies on the foreign key, so a set stored without one would
    leave the workspace advertising a pool that no longer exists.
    """

    async def test_deleting_a_pool_removes_it_from_every_set(self, app):
        ws, pools = await _seed(["cascade-a", "cascade-b"])

        async with get_db_session() as db:
            doomed = (
                await db.execute(select(AgentPool).where(AgentPool.id == pools[0].id))
            ).scalar_one()
            await db.delete(doomed)
            await db.commit()

        async with get_db_session() as db:
            refreshed = (
                await db.execute(select(Workspace).where(Workspace.id == ws.id))
            ).scalar_one()
            assert pool_set.workspace_pool_ids(refreshed) == [pools[1].id]

    async def test_deleting_the_last_pool_empties_the_set(self, app):
        ws, pools = await _seed(["solo-cascade"])

        async with get_db_session() as db:
            doomed = (
                await db.execute(select(AgentPool).where(AgentPool.id == pools[0].id))
            ).scalar_one()
            await db.delete(doomed)
            await db.commit()

        async with get_db_session() as db:
            refreshed = (
                await db.execute(select(Workspace).where(Workspace.id == ws.id))
            ).scalar_one()
            assert pool_set.workspace_pool_ids(refreshed) == []

    async def test_deleting_a_workspace_leaves_the_pool_alone(self, app):
        """CASCADE runs the other way too, without taking the pool with it."""
        ws, pools = await _seed(["ws-delete"])

        async with get_db_session() as db:
            doomed = (await db.execute(select(Workspace).where(Workspace.id == ws.id))).scalar_one()
            await db.delete(doomed)
            await db.commit()

        async with get_db_session() as db:
            survivors = list(
                (await db.execute(select(AgentPool).where(AgentPool.id == pools[0].id))).scalars()
            )
            assert len(survivors) == 1
