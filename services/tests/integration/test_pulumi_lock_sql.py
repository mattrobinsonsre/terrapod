"""Integration: the Pulumi workspace lock's SQL is valid against real Postgres.

Real Postgres, because that is the only thing that can fail here. The statement
these functions build is `SELECT ... FOR UPDATE` over `Workspace`, which eagerly
joins `vcs_connection` on a nullable foreign key — so the ORM renders a LEFT
OUTER JOIN and Postgres refuses:

    FOR UPDATE cannot be applied to the nullable side of an outer join

**Every mocked test of these functions passed while that was true**, which is
why this file exists rather than another unit test. It was found by an
agent-mode Pulumi apply on a live stack: `complete_update` 500'd on the release,
the CLI retried, and the retry got a 401 because the first call had already
dropped the Redis record — surfacing as `failed to complete update: [401]
Unknown or expired update`, which names neither the real error nor the table.

It is not a Pulumi-agent bug. A local `pulumi up` takes the same lock through
the same statement, so it was broken there too and simply had no live exercise.
"""

import uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import Workspace
from terrapod.db.session import get_db_session
from terrapod.services.pulumi_update_locks import (
    lock_id_for,
    release_workspace_lock,
    take_workspace_lock,
)

pytestmark = pytest.mark.integration


async def _workspace(*, with_vcs: bool = False) -> uuid.UUID:
    """A Pulumi workspace row. `with_vcs` is irrelevant to the bug and that is
    the point: the join is rendered from the mapping, not from the row's data,
    so a workspace with no VCS connection fails exactly the same way."""
    async with get_db_session() as db:
        ws = Workspace(
            name=f"lock-sql-{uuid.uuid4().hex[:8]}::dev",
            engine="pulumi",
            execution_mode="agent",
        )
        db.add(ws)
        await db.commit()
        return ws.id


class TestTheLockStatementRuns:
    async def test_taking_the_lock_does_not_raise(self) -> None:
        ws_id = await _workspace()
        update_id = str(uuid.uuid4())

        async with get_db_session() as db:
            await take_workspace_lock(db, ws_id, update_id)

        async with get_db_session() as db:
            ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
            assert ws.locked is True
            assert ws.lock_id == lock_id_for(update_id)

    async def test_releasing_it_does_not_raise(self) -> None:
        ws_id = await _workspace()
        update_id = str(uuid.uuid4())

        async with get_db_session() as db:
            await take_workspace_lock(db, ws_id, update_id)
        async with get_db_session() as db:
            assert await release_workspace_lock(db, ws_id, update_id) is True

        async with get_db_session() as db:
            ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
            assert ws.locked is False
            assert ws.lock_id is None

    async def test_releasing_a_lock_this_update_does_not_hold_leaves_it_alone(self) -> None:
        """The conditional release, over real rows: the guard that stops one
        update dropping another's lock has to survive the same statement."""
        ws_id = await _workspace()
        holder = str(uuid.uuid4())

        async with get_db_session() as db:
            await take_workspace_lock(db, ws_id, holder)
        async with get_db_session() as db:
            assert await release_workspace_lock(db, ws_id, str(uuid.uuid4())) is False

        async with get_db_session() as db:
            ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
            assert ws.locked is True
            assert ws.lock_id == lock_id_for(holder)
