"""Seeding several agent pools, against a real Postgres (#1411).

The unit tests cover reading the environment. Everything here needs the database:
whether pools and their tokens actually land, whether re-running the Job is the
no-op it claims to be, and what happens when a token is already registered to a
different pool — which is the case that silently leaves a listener unable to
join.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from terrapod.cli.bootstrap import PoolSpec, TokenLimits, _bootstrap_pool
from terrapod.db.models import AgentPool, AgentPoolToken
from terrapod.db.session import get_db_session
from terrapod.services.agent_pool_service import validate_join_token

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


class TestPartialFailure:
    """One bad pool must not hide the others, or take them down with it.

    The pre-flight duplicate check catches a token shared *within* one batch. It
    cannot see a token already registered from an earlier run, so that lands in
    the loop — which is exactly the case the per-pool error handling exists for.
    """

    async def test_the_job_fails_but_the_good_pools_are_still_seeded(self, app) -> None:
        await _seed(PoolSpec("existing", "already-used"))

        # 'fresh' is fine; 'clashing' reuses the token 'existing' already holds.
        async with get_db_session() as session, session.begin():
            failures = []
            for spec in (PoolSpec("fresh", "its-own"), PoolSpec("clashing", "already-used")):
                try:
                    await _bootstrap_pool(session, spec)
                except RuntimeError as exc:
                    failures.append(f"{spec.name}: {exc}")

        # The healthy pool landed rather than being rolled back with the bad one,
        # and the failure names which pool so it is actionable.
        assert len(failures) == 1 and "clashing" in failures[0]
        assert "fresh" in await _pool_names()

    async def test_a_rerun_after_fixing_the_token_completes_the_work(self, app) -> None:
        """Idempotency is what makes 'fix the Secret and re-run' a real answer."""
        await _seed(PoolSpec("existing", "already-used"), PoolSpec("fresh", "its-own"))

        with pytest.raises(RuntimeError):
            await _seed(PoolSpec("clashing", "already-used"))

        await _seed(
            PoolSpec("existing", "already-used"),
            PoolSpec("fresh", "its-own"),
            PoolSpec("clashing", "now-its-own"),
        )

        assert await _pool_names() == ["clashing", "existing", "fresh"]
        assert await _token_count() == 3


class TestTheBootstrapTokenIsBounded:
    """The row the bootstrap writes carries a use limit and an expiry.

    GHSA-93m3-v3h4-4qvw. `max_uses` and `expires_at` are columns, and the whole
    finding was that bootstrap left both null — a permanent, unlimited credential
    for joining a listener to the pool, and a listener in the pool receives every
    variable the runs it claims resolve. Mocking the session would prove the
    arguments were passed; only the real insert proves the row carries them.
    """

    async def _token(self) -> AgentPoolToken:
        async with get_db_session() as session:
            rows = await session.execute(select(AgentPoolToken))
            return rows.scalars().one()

    async def test_the_default_row_is_one_use_and_expires_within_a_day(self, app) -> None:
        before = datetime.now(UTC)
        await _seed(PoolSpec("bounded", "a-token"))

        token = await self._token()
        assert token.max_uses == 1
        assert token.use_count == 0
        assert token.expires_at is not None
        # A day, give or take the time the insert took. Asserting the window
        # rather than the exact instant, but tightly enough that an hour or a
        # year would both fail.
        delta = token.expires_at - before
        assert timedelta(hours=23, minutes=59) < delta < timedelta(hours=24, minutes=1)

    async def test_the_bounded_token_still_validates_on_its_first_use(self, app) -> None:
        """The bound has to be loose enough for the join it exists to allow.

        A limit that refuses the very first join would turn this fix into an
        install that never completes, and the pool would simply go quiet.
        """
        await _seed(PoolSpec("bounded", "a-token"))

        async with get_db_session() as session:
            assert await validate_join_token(session, "a-token") is not None

    async def test_a_spent_token_stops_validating(self, app) -> None:
        """One use means one, which is the property the advisory asked for."""
        await _seed(PoolSpec("bounded", "a-token"))

        async with get_db_session() as session, session.begin():
            token = (await session.execute(select(AgentPoolToken))).scalars().one()
            token.use_count = 1

        async with get_db_session() as session:
            assert await validate_join_token(session, "a-token") is None

    async def test_an_expired_token_stops_validating(self, app) -> None:
        await _seed(PoolSpec("bounded", "a-token"))

        async with get_db_session() as session, session.begin():
            token = (await session.execute(select(AgentPoolToken))).scalars().one()
            token.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        async with get_db_session() as session:
            assert await validate_join_token(session, "a-token") is None

    async def test_the_explicit_opt_out_writes_a_genuinely_unlimited_row(self, app) -> None:
        """`0` on either limit is an operator's deliberate "no bound".

        It has to reach the row as NULL, because that is what
        `validate_join_token` reads as unlimited — storing 0 would instead mean
        "zero uses allowed" and refuse every join.
        """
        async with get_db_session() as session, session.begin():
            await _bootstrap_pool(
                session,
                PoolSpec("unbounded", "a-token"),
                TokenLimits(max_uses=None, ttl_seconds=None),
            )

        token = await self._token()
        assert token.max_uses is None
        assert token.expires_at is None

    async def test_limits_are_applied_to_every_pool_in_a_list(self, app) -> None:
        """Not just the first — each pool gets its own bounded token."""
        await _seed(PoolSpec("pool-a", "tok-a"), PoolSpec("pool-b", "tok-b"))

        async with get_db_session() as session:
            rows = await session.execute(select(AgentPoolToken))
            tokens = list(rows.scalars().all())

        assert len(tokens) == 2
        assert all(t.max_uses == 1 and t.expires_at is not None for t in tokens)
