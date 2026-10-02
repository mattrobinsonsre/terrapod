"""A runner token stops working when its run ends (GHSA-xmrf-hxq9-m59m).

A runner token is a stateless HMAC, so verifying it says nothing about the run.
Before this it stayed usable for its whole TTL — up to two hours — after the run
reached a terminal state. Two mechanisms, tested separately because they cover
different failures: a revocation marker written on the terminal transition (which
makes it prompt) and a terminal-state check against the run row (which makes it
correct even when no marker was written).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.auth import runner_token_state as rts


@pytest.fixture
def _no_redis():
    """Redis unreachable — the row is then the authority."""
    with patch(
        "terrapod.redis.client.get_redis_client",
        side_effect=RuntimeError("redis not initialised"),
    ):
        yield


class _FakeRedis:
    def __init__(self, initial=None):
        self.store: dict[str, str] = dict(initial or {})
        self.sets: list[tuple[str, str, int | None]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.sets.append((key, value, ex))


def _db_with_status(status):
    """A session whose single-column select answers this run status."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = status
    db.execute = AsyncMock(return_value=result)
    return db


class TestTheTerminalStateCheck:
    async def test_a_live_run_is_usable(self, _no_redis):
        assert await rts.is_run_token_usable(str(uuid.uuid4()), _db_with_status("applying")) is True

    @pytest.mark.parametrize("status", sorted(rts.TERMINAL_STATES))
    async def test_every_terminal_status_refuses(self, _no_redis, status):
        assert await rts.is_run_token_usable(str(uuid.uuid4()), _db_with_status(status)) is False

    async def test_a_run_that_no_longer_exists_refuses(self, _no_redis):
        """A token for a deleted run is not a credential either."""
        assert await rts.is_run_token_usable(str(uuid.uuid4()), _db_with_status(None)) is False

    async def test_a_prefixed_run_id_is_accepted(self, _no_redis):
        """Every other run endpoint takes `run-{uuid}` as well as the bare uuid,
        and the token's own spelling has been a source of false refusals before
        (#1699). A spelling difference must not read as "run not found"."""
        rid = uuid.uuid4()
        assert await rts.is_run_token_usable(f"run-{rid}", _db_with_status("planning")) is True

    async def test_terminal_states_match_the_run_service(self):
        """The copy exists to keep `run_service` out of the auth import graph; it
        must not be allowed to drift from the real set."""
        from terrapod.services.run_service import TERMINAL_STATES

        assert rts.TERMINAL_STATES == frozenset(TERMINAL_STATES)


class TestTheRevocationMarker:
    async def test_a_marker_refuses_without_reading_the_row(self):
        """Prompt, and cheap: the marker is the fast path, so a revoked token
        costs one Redis read and no query."""
        run_id = str(uuid.uuid4())
        redis = _FakeRedis({f"tp:runtok_state:{run_id}": "revoked"})
        db = _db_with_status("planning")  # would say "usable" if consulted
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            assert await rts.is_run_token_usable(run_id, db) is False
        db.execute.assert_not_awaited()

    async def test_revoke_writes_a_marker_that_outlives_any_token(self):
        run_id = uuid.uuid4()
        redis = _FakeRedis()
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            await rts.revoke_run_tokens(run_id)
        assert redis.store[f"tp:runtok_state:{run_id}"] == "revoked"
        (_key, _value, ex) = redis.sets[0]
        # Longer than the maximum life of any token for that run, or the marker
        # could lapse while a token it revokes is still signature-valid.
        assert ex is not None and ex > 7200

    async def test_revoking_never_raises(self):
        """It is called from inside the run state machine. A run must reach its
        terminal state whether or not Redis is reachable."""
        with patch(
            "terrapod.redis.client.get_redis_client",
            side_effect=RuntimeError("redis is away"),
        ):
            await rts.revoke_run_tokens(uuid.uuid4())  # must not raise

    async def test_an_active_answer_is_cached_so_a_runner_is_not_queried_twice(self):
        run_id = str(uuid.uuid4())
        redis = _FakeRedis()
        db = _db_with_status("planning")
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            assert await rts.is_run_token_usable(run_id, db) is True
            assert await rts.is_run_token_usable(run_id, db) is True
        assert db.execute.await_count == 1
        assert redis.store[f"tp:runtok_state:{run_id}"] == "active"
        assert redis.sets[0][2] == rts._ACTIVE_TTL

    async def test_a_terminal_run_with_no_marker_gets_one(self):
        """Self-healing, and it is what covers a run that ended before this
        shipped or whose marker write failed: the row check writes the marker so
        the next request is answered from Redis."""
        run_id = str(uuid.uuid4())
        redis = _FakeRedis()
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            assert await rts.is_run_token_usable(run_id, _db_with_status("applied")) is False
        assert redis.store[f"tp:runtok_state:{run_id}"] == "revoked"


class TestItFailsOpenOnInfrastructureAndClosedOnAnAnswer:
    async def test_no_redis_and_no_session_allows(self):
        """Nothing can be consulted. Refusing here would stop every live run on a
        transient Redis blip, which is worse than a token lasting a few seconds
        longer — and the marker path still refuses a revoked token when Redis
        comes back."""
        with patch(
            "terrapod.redis.client.get_redis_client",
            side_effect=RuntimeError("redis is away"),
        ):
            assert await rts.is_run_token_usable(str(uuid.uuid4()), None) is True

    async def test_a_marker_is_honoured_even_with_no_session(self):
        run_id = str(uuid.uuid4())
        redis = _FakeRedis({f"tp:runtok_state:{run_id}": "revoked"})
        with patch("terrapod.redis.client.get_redis_client", return_value=redis):
            assert await rts.is_run_token_usable(run_id, None) is False

    async def test_an_unreadable_row_allows(self):
        """A failed query is an infrastructure error, not an answer."""
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("connection reset"))
        with patch(
            "terrapod.redis.client.get_redis_client",
            side_effect=RuntimeError("redis is away"),
        ):
            assert await rts.is_run_token_usable(str(uuid.uuid4()), db) is True

    async def test_a_malformed_run_id_refuses(self):
        """It cannot name a run, so it cannot name a live one."""
        with patch(
            "terrapod.redis.client.get_redis_client",
            side_effect=RuntimeError("redis is away"),
        ):
            assert await rts.is_run_token_usable("not-a-uuid", _db_with_status("planning")) is False
