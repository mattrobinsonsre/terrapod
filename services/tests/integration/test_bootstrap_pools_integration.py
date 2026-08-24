"""Seeding several agent pools, against a real Postgres (#1411).

The unit tests cover reading the environment. Everything here needs the database:
whether pools and their tokens actually land, whether re-running the Job is the
no-op it claims to be, and what happens when a token is already registered to a
different pool — which is the case that silently leaves a listener unable to
join.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import func, select

from terrapod.cli.bootstrap import PoolSpec, _bootstrap_pool
from terrapod.db.models import AgentPool, AgentPoolToken
from terrapod.db.session import get_db_session

pytestmark = pytest.mark.asyncio


async def _seed(*specs: PoolSpec) -> None:
    async with get_db_session() as session, session.begin():
        for spec in specs:
            await _bootstrap_pool(session, spec)


async def _pool_names() -> list[str]:
    async with get_db_session() as session:
        rows = await session.execute(select(AgentPool.name).order_by(AgentPool.name))
        return list(rows.scalars().all())


async def _token_count() -> int:
    async with get_db_session() as session:
        return await session.scalar(select(func.count()).select_from(AgentPoolToken)) or 0


class TestSeedingSeveralPools:
    async def test_each_pool_is_created_with_its_own_token(self, app) -> None:
        await _seed(PoolSpec("pool-a", "tok-a"), PoolSpec("pool-b", "tok-b"))

        assert await _pool_names() == ["pool-a", "pool-b"]
        assert await _token_count() == 2

    async def test_the_token_is_stored_hashed(self, app) -> None:
        """A join token is a credential; the row must not carry it in the clear."""
        await _seed(PoolSpec("pool-a", "tok-a"))

        async with get_db_session() as session:
            token = (await session.execute(select(AgentPoolToken))).scalar_one()
        assert token.token_hash == hashlib.sha256(b"tok-a").hexdigest()
        assert "tok-a" not in str(token.__dict__)

    async def test_re_running_is_a_no_op(self, app) -> None:
        """The Job runs on every upgrade, so this is the common case, not an edge.

        Duplicating pools or tokens on each upgrade would be a slow-motion mess
        that only shows up weeks later.
        """
        await _seed(PoolSpec("pool-a", "tok-a"), PoolSpec("pool-b", "tok-b"))
        await _seed(PoolSpec("pool-a", "tok-a"), PoolSpec("pool-b", "tok-b"))

        assert await _pool_names() == ["pool-a", "pool-b"]
        assert await _token_count() == 2

    async def test_a_pool_added_later_joins_the_existing_ones(self, app) -> None:
        """Growing the list on an upgrade is the whole point of the feature."""
        await _seed(PoolSpec("pool-a", "tok-a"))
        await _seed(PoolSpec("pool-a", "tok-a"), PoolSpec("pool-b", "tok-b"))

        assert await _pool_names() == ["pool-a", "pool-b"]
        assert await _token_count() == 2


class TestTokenAlreadyRegisteredElsewhere:
    """The failure that would otherwise be silent.

    `token_hash` is unique across all pools, and registration skips a hash that
    already exists. Without this check the second pool is created, its token
    quietly skipped, and it ends up with none — the Job reports success and a
    listener never joins, with nothing in the logs pointing at why.
    """

    async def test_reusing_another_pools_token_is_an_error(self, app) -> None:
        await _seed(PoolSpec("pool-a", "shared"))

        with pytest.raises(RuntimeError, match="already registered to a different pool"):
            await _seed(PoolSpec("pool-b", "shared"))

    async def test_the_first_pool_keeps_its_token(self, app) -> None:
        """The error must not have taken the working pool down with it."""
        await _seed(PoolSpec("pool-a", "shared"))
        with pytest.raises(RuntimeError):
            await _seed(PoolSpec("pool-b", "shared"))

        async with get_db_session() as session:
            pool = (
                await session.execute(select(AgentPool).where(AgentPool.name == "pool-a"))
            ).scalar_one()
            token = (await session.execute(select(AgentPoolToken))).scalar_one()
        assert token.pool_id == pool.id
