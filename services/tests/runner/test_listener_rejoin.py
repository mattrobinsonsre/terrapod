"""The listener's recovery from "the API no longer accepts this cert".

`_rejoin` is the only automatic recovery from a rejected certificate, and
before these tests existed nothing exercised it — no test in the tree referred
to the re-join, its threshold, or the counter that drives it.

What that cost, measured on a live stack: the API restarted across a cert
renewal window, renewal failed, and an expired cert cannot renew itself. The
listener then looped for 25 minutes — registered nowhere, no run of any kind
dispatchable — and recovered only when its credentials Secret was deleted by
hand. It never attempted a re-join, because the SSE loop's counter was reset by
*any* non-401 error, and it had recorded 2 auth failures against 22 transport
failures.

So the property under test is not "three 401s re-join". It is that the count
survives the failures that are not auth failures, and that the renewal loop's
rejections — the authoritative signal, since a 401 from /renew is a decision
the API declined to retry — count towards the same recovery.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from terrapod.runner import listener as listener_mod
from terrapod.runner.listener import _REJOIN_THRESHOLD, RunnerListener


def _make_listener() -> RunnerListener:
    listener = RunnerListener()
    listener.identity = SimpleNamespace(
        listener_id="11111111-1111-1111-1111-111111111111",
        name="listener",
        api_url="http://test",
        certificate_pem="",
    )
    listener._sse_retry_interval = 0.001
    return listener


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://test/events")
    return httpx.HTTPStatusError(
        f"{code}", request=request, response=httpx.Response(code, request=request)
    )


async def _drive_sse(listener: RunnerListener, outcomes: list) -> AsyncMock:
    """Run `_sse_loop` over a scripted list of `_sse_connect` outcomes.

    An entry is an exception to raise, or None for a clean connection. The loop
    is stopped by setting the module shutdown flag as the last entry is served,
    which is also how the real listener leaves it.
    """
    rejoin = AsyncMock()
    calls = {"n": 0}
    # A fresh Event per test. The module global binds itself to the first event
    # loop that awaits it, so a second test on a new loop would raise
    # "bound to a different event loop" rather than exercise anything.
    shutdown = asyncio.Event()

    async def fake_connect():
        calls["n"] += 1
        if calls["n"] >= len(outcomes):
            shutdown.set()
        outcome = outcomes[min(calls["n"], len(outcomes)) - 1]
        if outcome is not None:
            raise outcome

    with (
        patch.object(listener_mod, "_shutdown", shutdown),
        patch.object(listener, "_sse_connect", side_effect=fake_connect),
        patch.object(listener, "_rejoin_unlocked", rejoin),
    ):
        await asyncio.wait_for(listener._sse_loop(), timeout=10)
    return rejoin


class TestTheCountSurvivesWhatIsNotAnAuthFailure:
    @pytest.mark.asyncio
    async def test_a_transport_failure_between_401s_does_not_clear_the_count(self):
        """The defect, exactly: three 401s interleaved with a transport failure.

        Pre-fix this re-joined never — the `except Exception` branch set the
        counter back to 0, so a listener that was persistently unauthenticated
        *and* whose connections kept failing could not reach the threshold.
        """
        listener = _make_listener()
        rejoin = await _drive_sse(
            listener,
            [
                _status_error(401),
                httpx.ConnectError("transport", request=httpx.Request("GET", "http://t")),
                _status_error(401),
                _status_error(401),
            ],
        )
        assert rejoin.await_count == 1, (
            "three authoritative 401s must re-join even though a transport "
            "failure landed between them"
        )

    @pytest.mark.asyncio
    async def test_transport_failures_alone_never_re_join(self):
        """The negative path: a re-join is for auth rejections, nothing else.

        Without this, the test above is satisfied by a counter that increments
        on every error, which would spend join-token uses on network blips.
        """
        listener = _make_listener()
        err = httpx.ConnectError("transport", request=httpx.Request("GET", "http://t"))
        rejoin = await _drive_sse(listener, [err, err, err, err, err])
        assert rejoin.await_count == 0
        assert listener._auth_failures == 0

    @pytest.mark.asyncio
    async def test_a_successful_connection_clears_the_count(self):
        """Getting in means the cert is good, so prior rejections are stale."""
        listener = _make_listener()
        rejoin = await _drive_sse(
            listener,
            [_status_error(401), _status_error(401), None, _status_error(401), None],
        )
        assert rejoin.await_count == 0, (
            "two 401s then a success then one 401 is not three consecutive "
            "rejections, and must not re-join"
        )


class TestTheRenewalLoopFeedsTheSameRecovery:
    @pytest.mark.asyncio
    async def test_a_renew_rejection_counts_towards_the_same_threshold(self):
        """Two SSE 401s plus one renewal rejection reach the threshold.

        The renewal loop holds the authoritative signal — `/renew` does not
        retry a 401 — so its rejections must not be counted separately. One
        threshold, one re-join, whichever loop sees the rejection.
        """
        listener = _make_listener()
        with patch.object(listener, "_rejoin_unlocked", AsyncMock()) as rejoin:
            assert await listener._note_auth_failure(source="sse") is False
            assert await listener._note_auth_failure(source="sse") is False
            assert rejoin.await_count == 0
            assert await listener._note_auth_failure(source="renew") is True
            assert rejoin.await_count == 1

    @pytest.mark.asyncio
    async def test_a_failed_re_join_reports_false_and_does_not_clear_the_count(self):
        """A re-join that raises must not look like a recovery."""
        listener = _make_listener()
        listener._auth_failures = _REJOIN_THRESHOLD - 1
        with patch.object(listener, "_rejoin_unlocked", AsyncMock(side_effect=RuntimeError("no"))):
            assert await listener._note_auth_failure(source="sse") is False
        assert listener._auth_failures == _REJOIN_THRESHOLD, (
            "the count must survive a failed re-join, or the next rejection "
            "starts again from zero and recovery is pushed further away"
        )


class TestReJoinIsSerialised:
    @pytest.mark.asyncio
    async def test_two_concurrent_re_joins_perform_one(self):
        """Two reporters must not each consume a join-token use.

        `_rejoin_unlocked` awaits, so the two calls genuinely interleave — a
        race written sequentially is not a race. On a pool whose token has
        `max_uses: 1`, a second join is the difference between a recovered
        listener and one that can never join again.
        """
        listener = _make_listener()
        ran = {"n": 0}

        async def slow_rejoin():
            ran["n"] += 1
            await asyncio.sleep(0)

        with patch.object(listener, "_rejoin_unlocked", side_effect=slow_rejoin):
            await asyncio.gather(listener._rejoin(), listener._rejoin())

        assert ran["n"] == 1, "the second caller must see the generation move and skip"
        assert listener._rejoin_generation == 1


class TestTheRejoinMechanismIsCalledCorrectly:
    """The gate above patches `_rejoin_unlocked`, so nothing here exercised it.

    Two properties of the CALL, which no test of `rejoin_identity` itself can
    see: that the listener hands it the identity it holds, and that a failure
    propagates rather than being swallowed into a partial state.
    """

    @pytest.mark.asyncio
    async def test_the_held_identity_is_passed_so_a_sibling_can_be_distinguished(self):
        """`current=None` would make `rejoin_identity` adopt our own rejected cert.

        The resourceVersion we hold is the only thing separating "a sibling has
        re-joined, adopt it" from "the Secret still holds the cert being
        rejected", so dropping this argument is silently fatal: the listener
        would adopt the rejected cert for ever and never join.
        """
        listener = _make_listener()
        fresh = SimpleNamespace(
            listener_id="22222222-2222-2222-2222-222222222222",
            name="listener",
            api_url="http://test",
            certificate_pem="",
        )
        rejoin = AsyncMock(return_value=fresh)

        with patch("terrapod.runner.identity.rejoin_identity", rejoin):
            await listener._rejoin_unlocked()

        assert rejoin.await_args.kwargs["current"] is not None
        assert listener.identity is fresh
        assert listener._identity_ready is True
        # The cached headers are keyed on the old cert and must not survive.
        assert listener._cached_auth_headers_for_cert is None

    @pytest.mark.asyncio
    async def test_a_failed_rejoin_propagates_and_leaves_the_old_identity(self):
        listener = _make_listener()
        held = listener.identity

        with patch(
            "terrapod.runner.identity.rejoin_identity",
            AsyncMock(side_effect=RuntimeError("join refused")),
        ):
            with pytest.raises(RuntimeError, match="join refused"):
                await listener._rejoin_unlocked()

        # Unchanged: the caller's `except Exception` turns this into a False, and
        # the listener keeps the identity it had rather than holding none.
        assert listener.identity is held
