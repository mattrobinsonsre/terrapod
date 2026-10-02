"""The absolute session ceiling, and the two ways a role change reaches a session.

GHSA-pwrq-j4cv-w7qg. Three properties, each pinned against a Redis fake that
actually expires keys rather than an `AsyncMock` that records calls:

* a sliding refresh cannot push a session past a deadline measured from login;
* a reduction ends the sessions it affects and nothing else;
* a widening adds roles to a live session without ending it or moving its expiry.

The fake matters. `refresh_session` reads the stored record, recomputes a TTL and
writes it back, so a mock that returns whatever it is told cannot tell a clamped
TTL from an unclamped one — it would assert that `set` was called and pass either
way. This one carries real expiries, so "the session is still alive after 30
hours" is a thing a test can be wrong about.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from terrapod.auth import sessions as sessions_module
from terrapod.auth.sessions import (
    SESSION_PREFIX,
    USER_SESSIONS_PREFIX,
    Session,
    create_session,
    get_session,
    grant_roles_to_user_sessions,
    refresh_session,
    revoke_user_sessions,
)
from terrapod.config import settings
from terrapod.db.models import now_utc
from tests.fake_redis import FakeRedis


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    """A Redis fake whose clock the module under test shares.

    `advance()` has to move BOTH the key expiries and the wall clock the deadline
    is compared against, or the two disagree and the test proves nothing: Redis
    would drop a key the code still considers live, or the reverse.
    """
    fake = FakeRedis()
    monkeypatch.setattr(sessions_module, "get_redis_client", lambda: fake)
    monkeypatch.setattr(
        sessions_module,
        "now_utc",
        lambda: now_utc() + timedelta(seconds=fake.offset),
    )
    return fake


@pytest.fixture
def ceiling(monkeypatch):
    """Set the sliding window and the ceiling in hours, returning nothing."""

    def _set(sliding_hours: int, absolute_hours: int) -> None:
        monkeypatch.setattr(settings.auth, "session_ttl_hours", sliding_hours)
        monkeypatch.setattr(settings.auth, "session_absolute_ttl_hours", absolute_hours)

    return _set


async def _make(redis: FakeRedis, *, roles=("everyone",), provider="local", email="u@example.com"):
    return await create_session(
        email=email,
        display_name="U",
        roles=list(roles),
        provider_name=provider,
    )


def _stored(redis: FakeRedis, token: str) -> dict:
    return json.loads(redis.values[SESSION_PREFIX + token])


class TestTheCeilingIsRecordedAtLogin:
    async def test_a_session_carries_an_absolute_deadline(self, redis, ceiling):
        ceiling(12, 24)
        session = await _make(redis)

        assert session.absolute_expires_at != ""
        recorded = sessions_module._parse_iso(session.absolute_expires_at)
        assert recorded is not None
        expected = now_utc() + timedelta(hours=24)
        assert abs((recorded - expected).total_seconds()) < 5
        # And it is persisted, not just returned — the next replica to touch this
        # session reads it out of Redis.
        assert _stored(redis, session.token)["absolute_expires_at"] != ""

    async def test_the_redis_ttl_never_outlives_the_ceiling(self, redis, ceiling):
        """A ceiling shorter than the sliding window is still the ceiling."""
        ceiling(12, 2)
        session = await _make(redis)

        assert await redis.ttl(SESSION_PREFIX + session.token) <= 2 * 3600

    async def test_zero_means_no_ceiling(self, redis, ceiling):
        ceiling(12, 0)
        session = await _make(redis)

        assert session.absolute_expires_at == ""
        assert sessions_module.absolute_deadline(session) is None


class TestASlidingRefreshCannotOutrunTheCeiling:
    """The property the advisory is about, driven through repeated refreshes."""

    async def test_polling_for_days_does_not_keep_the_session_alive(self, redis, ceiling):
        ceiling(12, 24)
        session = await _make(redis)

        # A browser that keeps the session warm: refresh every six hours.
        for _ in range(12):
            redis.advance(6 * 3600)
            stored = await get_session(session.token)
            if stored is None:
                break
            await refresh_session(session.token, stored)

        assert await get_session(session.token) is None, (
            "a session survived past its absolute deadline, so the sliding "
            "window is re-arming without being clamped"
        )

    async def test_a_refresh_is_clamped_to_what_is_left(self, redis, ceiling):
        # Sliding window longer than the ceiling, so the key is still there at
        # the moment the clamp has to bite.
        ceiling(48, 24)
        session = await _make(redis)

        # 23h45m in: 15 minutes of the absolute window remain, against a 48h
        # sliding window.
        redis.advance(24 * 3600 - 900)
        stored = await get_session(session.token)
        assert stored is not None

        new_expires = await refresh_session(session.token, stored)

        remaining = await redis.ttl(SESSION_PREFIX + session.token)
        assert 0 < remaining <= 900, remaining
        deadline = sessions_module.absolute_deadline(stored)
        assert deadline is not None
        assert sessions_module._parse_iso(new_expires) <= deadline

    async def test_a_refresh_past_the_deadline_revokes_instead_of_rearming(self, redis, ceiling):
        """The deadline is enforced even by the code whose job is to extend."""
        ceiling(12, 24)
        session = await _make(redis)
        stored = await get_session(session.token)
        assert stored is not None

        redis.advance(25 * 3600)
        await refresh_session(session.token, stored)

        assert SESSION_PREFIX + session.token not in redis.values


class TestTheDeadlineBindsOnEveryRequest:
    """The three ways a live Redis key can outlast the deadline it must obey.

    Each of these has to hand-extend the key's TTL, and that is the point rather
    than a convenience: `create_session` clamps the TTL to the ceiling, so a
    session created by the current code dies of its own expiry and `get_session`'s
    check never runs. Letting Redis do the work here would be a test that passes
    whether or not the check exists — which is exactly what it did before this
    comment was written.
    """

    @staticmethod
    def _old_key_ttl(redis: FakeRedis, token: str, hours: int) -> None:
        """Give the key the TTL the unclamped code would have written."""
        redis.deadlines[SESSION_PREFIX + token] = redis.now() + hours * 3600

    async def test_get_session_refuses_and_deletes_an_expired_session(self, redis, ceiling):
        ceiling(48, 24)
        session = await _make(redis)
        self._old_key_ttl(redis, session.token, 48)
        redis.advance(25 * 3600)

        # The key is still in Redis with time left on it, so refusing the session
        # is this function's decision and nothing else's.
        assert redis.values.get(SESSION_PREFIX + session.token) is not None
        assert await redis.ttl(SESSION_PREFIX + session.token) > 0

        assert await get_session(session.token) is None
        # Not merely refused — removed, so the next request does not re-read it.
        assert SESSION_PREFIX + session.token not in redis.values

    async def test_a_record_written_before_the_field_existed_is_still_capped(self, redis, ceiling):
        """The sessions that predate the fix must not be the exempt ones."""
        ceiling(48, 24)
        session = await _make(redis)
        # The old record shape: no absolute_expires_at, and the full sliding TTL.
        data = _stored(redis, session.token)
        del data["absolute_expires_at"]
        redis.values[SESSION_PREFIX + session.token] = json.dumps(data)
        self._old_key_ttl(redis, session.token, 48)

        assert await get_session(session.token) is not None
        redis.advance(25 * 3600)
        assert await get_session(session.token) is None

    async def test_a_naive_timestamp_does_not_500_every_request(self, redis, ceiling):
        """Comparing an aware `now` against a naive stored value raises, and the
        comparison happens in `get_session` — so one malformed record would break
        every request that user makes rather than just its own session."""
        ceiling(48, 24)
        session = await _make(redis)
        data = _stored(redis, session.token)
        data["created_at"] = "2026-01-01T00:00:00"  # no offset
        data["absolute_expires_at"] = "2026-01-02T00:00:00"
        redis.values[SESSION_PREFIX + session.token] = json.dumps(data)
        self._old_key_ttl(redis, session.token, 48)

        assert await get_session(session.token) is None  # long past, not a crash

    async def test_lowering_the_ceiling_tightens_a_live_session(self, redis, ceiling):
        """An operator who shortens it means it for the sessions already open."""
        ceiling(48, 24)
        session = await _make(redis)
        redis.advance(3 * 3600)
        assert await get_session(session.token) is not None

        ceiling(48, 2)
        assert await get_session(session.token) is None

    async def test_an_expired_session_is_not_listed_as_active(self, redis, ceiling):
        """An admin checking whether a demoted user is still signed in gets the
        truth, not a row for a session that can no longer authenticate."""
        ceiling(48, 24)
        session = await _make(redis)
        self._old_key_ttl(redis, session.token, 48)
        redis.advance(25 * 3600)

        assert redis.values.get(SESSION_PREFIX + session.token) is not None
        assert await sessions_module.list_user_sessions(session.email) == []
        assert await sessions_module.list_all_sessions() == []


class TestRevokingOneProvidersSessions:
    async def test_only_the_named_providers_sessions_end(self, redis, ceiling):
        ceiling(12, 24)
        local = await _make(redis, provider="local")
        oidc = await _make(redis, provider="okta")

        count = await revoke_user_sessions(local.email, provider_name="local")

        assert count == 1
        assert await get_session(local.token) is None
        assert await get_session(oidc.token) is not None

    async def test_the_index_keeps_the_surviving_session(self, redis, ceiling):
        """Deleting the whole set would orphan the other provider's session."""
        ceiling(12, 24)
        local = await _make(redis, provider="local")
        oidc = await _make(redis, provider="okta")

        await revoke_user_sessions(local.email, provider_name="local")

        assert await redis.smembers(USER_SESSIONS_PREFIX + local.email) == {oidc.token}

    async def test_no_provider_means_all_of_them(self, redis, ceiling):
        ceiling(12, 24)
        local = await _make(redis, provider="local")
        oidc = await _make(redis, provider="okta")

        await revoke_user_sessions(local.email)

        assert await get_session(local.token) is None
        assert await get_session(oidc.token) is None


class TestGrantingRolesToALiveSession:
    async def test_the_new_role_appears_without_a_logout(self, redis, ceiling):
        ceiling(12, 24)
        session = await _make(redis, roles=["everyone"])

        touched = await grant_roles_to_user_sessions(
            session.email, {"deployer"}, provider_name="local"
        )

        assert touched == 1
        refreshed = await get_session(session.token)
        assert refreshed is not None
        assert refreshed.roles == ["deployer", "everyone"]

    async def test_roles_from_the_idp_survive(self, redis, ceiling):
        """A union, not a re-resolution.

        Two of login's three role sources need the IdP's token and are gone by
        now, so recomputing from the stored assignments would silently strip
        them. This is the test that fails if someone "simplifies" the union into
        a fresh `process_login`-style resolve.
        """
        ceiling(12, 24)
        session = await _make(redis, roles=["everyone", "from-idp-group"], provider="okta")

        await grant_roles_to_user_sessions(session.email, {"deployer"}, provider_name="okta")

        refreshed = await get_session(session.token)
        assert refreshed is not None
        assert "from-idp-group" in refreshed.roles
        assert "deployer" in refreshed.roles

    async def test_the_expiry_does_not_move(self, redis, ceiling):
        """A widening must neither extend a session nor shorten it."""
        ceiling(12, 24)
        session = await _make(redis)
        redis.advance(3600)
        before = await redis.ttl(SESSION_PREFIX + session.token)

        await grant_roles_to_user_sessions(session.email, {"deployer"})

        after = await redis.ttl(SESSION_PREFIX + session.token)
        assert abs(after - before) <= 1, (before, after)

    async def test_another_providers_session_is_left_alone(self, redis, ceiling):
        ceiling(12, 24)
        local = await _make(redis, provider="local")
        oidc = await _make(redis, provider="okta")

        await grant_roles_to_user_sessions(local.email, {"deployer"}, provider_name="local")

        other = await get_session(oidc.token)
        assert other is not None
        assert "deployer" not in other.roles

    async def test_an_expiring_key_is_not_resurrected(self, redis, ceiling):
        """Writing a dead key back with a fresh TTL would undo an expiry."""
        ceiling(12, 24)
        session = await _make(redis)
        redis.deadlines[SESSION_PREFIX + session.token] = redis.now() - 1

        touched = await grant_roles_to_user_sessions(session.email, {"deployer"})

        assert touched == 0
        assert SESSION_PREFIX + session.token not in redis.values

    async def test_granting_nothing_touches_nothing(self, redis, ceiling):
        ceiling(12, 24)
        session = await _make(redis)

        assert await grant_roles_to_user_sessions(session.email, set()) == 0
        assert await grant_roles_to_user_sessions(session.email, {"everyone"}) == 0


class TestTheCeilingIsWiredToConfig:
    def test_the_default_is_a_real_ceiling_above_the_sliding_window(self) -> None:
        """A ceiling below the sliding window would make the window pointless,
        and 0 would mean no ceiling at all — which is what 2.0 moves away from."""
        from terrapod.config import AuthConfig

        auth = AuthConfig()
        assert auth.session_absolute_ttl_hours > 0
        assert auth.session_absolute_ttl_hours >= auth.session_ttl_hours

    def test_a_session_record_round_trips_through_redis(self) -> None:
        """`get_session` rebuilds a Session from the stored dict by keyword, so a
        field added without a default breaks every existing session at once."""
        import dataclasses

        stored = dataclasses.asdict(
            Session(
                email="u@example.com",
                display_name=None,
                roles=[],
                provider_name="local",
                created_at="2026-01-01T00:00:00+00:00",
                expires_at="2026-01-01T12:00:00+00:00",
                last_active_at="2026-01-01T00:00:00+00:00",
            )
        )
        stored.pop("token")
        assert Session(token="t", **stored).absolute_expires_at == ""
