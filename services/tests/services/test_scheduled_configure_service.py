"""Scheduled configure runs (#2538).

The cron evaluation is driven for real rather than mocked: the whole argument
for cron over drift detection's interval is that it is calendar-anchored, and a
fake that returns a fixed "next" proves nothing about that.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import scheduled_configure_service as svc
from terrapod.services.workspace_settings import (
    MIN_SCHEDULE_INTERVAL_SECONDS,
    validate_schedule_cron,
    validate_schedule_timezone,
)


def _ws(**kw):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = kw.get("name", "ws")
    ws.locked = kw.get("locked", False)
    ws.created_at = kw.get("created_at", datetime(2026, 1, 1, tzinfo=UTC))
    ws.scheduled_configure_enabled = kw.get("enabled", True)
    ws.scheduled_configure_cron = kw.get("cron", "0 2 * * *")
    ws.scheduled_configure_timezone = kw.get("timezone", "UTC")
    ws.scheduled_configure_last_at = kw.get("last_at", None)
    return ws


class TestTheScheduleIsCalendarAnchored:
    """The property that decided cron over an interval."""

    def test_an_hour_local_stays_that_hour_across_a_dst_change(self):
        """02:00 in London is 02:00 in London in January AND in July.

        In UTC those are different instants -- 02:00Z and 01:00Z -- which is
        exactly what an interval cannot express, and the reason the zone is
        carried rather than the schedule being stored as a UTC offset.
        """
        winter = svc.next_occurrence(
            "0 2 * * *", "Europe/London", datetime(2026, 1, 1, 12, tzinfo=UTC)
        )
        summer = svc.next_occurrence(
            "0 2 * * *", "Europe/London", datetime(2026, 7, 1, 12, tzinfo=UTC)
        )
        assert winter.hour == 2, winter
        assert summer.hour == 1, summer

    def test_a_weekday_schedule_lands_on_that_weekday(self):
        nxt = svc.next_occurrence("0 2 * * 2", "UTC", datetime(2026, 11, 13, 9, tzinfo=UTC))
        assert nxt.weekday() == 1, nxt  # Monday is 0, so Tuesday is 1

    @pytest.mark.parametrize("bad", ["nonsense", "", "* * *"])
    def test_an_unusable_expression_yields_no_occurrence_rather_than_raising(self, bad):
        """A stored value that cannot be evaluated must not wedge the cycle for
        every OTHER workspace, so this returns None instead of raising."""
        assert svc.next_occurrence(bad, "UTC", datetime(2026, 1, 1, tzinfo=UTC)) is None

    def test_an_unusable_zone_falls_back_rather_than_raising(self):
        assert (
            svc.next_occurrence("0 2 * * *", "Mars/Olympus", datetime(2026, 1, 1, tzinfo=UTC))
            is not None
        )


class TestDue:
    def test_it_is_measured_from_the_last_firing(self):
        now = datetime(2026, 3, 10, 2, 30, tzinfo=UTC)
        assert svc.is_due(_ws(last_at=datetime(2026, 3, 9, 2, 0, tzinfo=UTC)), now)
        assert not svc.is_due(_ws(last_at=datetime(2026, 3, 10, 2, 0, tzinfo=UTC)), now)

    def test_a_long_outage_fires_once_rather_than_catching_up(self):
        """Three missed days is ONE run, not three.

        A configure can run for an hour, so a catch-up would be a fleet-wide
        event nobody asked for. `is_due` is a boolean by construction, and the
        cycle stamps `now` rather than the occurrence that came due -- the two
        together are what make this true.
        """
        now = datetime(2026, 3, 13, 2, 30, tzinfo=UTC)
        ws = _ws(last_at=datetime(2026, 3, 10, 2, 0, tzinfo=UTC))
        assert svc.is_due(ws, now) is True

    def test_a_workspace_that_never_fired_is_measured_from_creation(self):
        ws = _ws(last_at=None, created_at=datetime(2026, 1, 1, tzinfo=UTC))
        assert svc.is_due(ws, datetime(2026, 1, 2, 3, tzinfo=UTC))


class TestTheCycle:
    async def _run_cycle(self, ws, *, in_flight=False, created=True):
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=lambda: [ws])))
        )
        db.commit = AsyncMock()
        run = MagicMock(id=uuid.uuid4()) if created else None
        with (
            patch.object(svc, "get_db_session") as session,
            patch.object(
                svc.inventory_auto_configure,
                "configure_in_flight",
                AsyncMock(return_value=in_flight),
            ),
            patch.object(svc.run_service, "create_run", AsyncMock(return_value=run)),
            patch.object(svc.run_service, "queue_run", AsyncMock(return_value=run)) as queue,
        ):
            session.return_value.__aenter__ = AsyncMock(return_value=db)
            session.return_value.__aexit__ = AsyncMock(return_value=False)
            await svc.scheduled_configure_cycle()
        return queue, db

    @pytest.mark.asyncio
    async def test_a_due_schedule_is_created_AND_QUEUED(self):
        """`create_run` leaves a run `pending`, which nothing sweeps.

        Without the queue the feature is inert AND self-blocking: the run sits
        at `pending` for ever, and because the in-flight predicate counts
        `pending` every later cycle then sees a configure already on its way and
        skips. One dead run per workspace, then nothing. The sibling creator
        learned this first; this asserts we did not relearn it.
        """
        queue, db = await self._run_cycle(_ws(last_at=datetime(2020, 1, 1, tzinfo=UTC)))
        assert queue.await_count == 1
        assert db.commit.await_count == 1

    @pytest.mark.asyncio
    async def test_a_locked_workspace_is_skipped(self):
        queue, db = await self._run_cycle(
            _ws(locked=True, last_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        assert queue.await_count == 0

    @pytest.mark.asyncio
    async def test_a_configure_already_on_its_way_is_skipped_WITHOUT_stamping(self):
        """The skip must not advance the stamp.

        Stamping here would treat a collision as a firing, so a schedule that
        ran into a long configure would silently lose that occurrence AND have
        its next one measured from the wrong moment.
        """
        ws = _ws(last_at=datetime(2020, 1, 1, tzinfo=UTC))
        before = ws.scheduled_configure_last_at
        queue, db = await self._run_cycle(ws, in_flight=True)
        assert queue.await_count == 0
        assert ws.scheduled_configure_last_at == before
        assert db.commit.await_count == 0

    @pytest.mark.asyncio
    async def test_a_schedule_not_yet_due_does_nothing(self):
        queue, _ = await self._run_cycle(_ws(last_at=datetime(2026, 12, 31, tzinfo=UTC)))
        assert queue.await_count == 0


class TestValidation:
    @pytest.mark.parametrize(
        "expr",
        ["0 2 * * *", "0 2 * * 2", "*/5 * * * *", "0 0 1 1 *", "", "   "],
    )
    def test_a_usable_expression_is_accepted_and_stored_verbatim(self, expr):
        assert validate_schedule_cron(expr) == expr.strip()

    @pytest.mark.parametrize(
        "expr",
        [
            "*/1 * * * *",  # every minute
            "0,1 * * * *",  # 60s apart, and NOT visible in the first two occurrences
            "nonsense",
            "*/5 * * * * *",  # six fields: seconds
        ],
    )
    def test_an_unusable_expression_is_refused(self, expr):
        with pytest.raises(ValueError):
            validate_schedule_cron(expr)

    def test_the_floor_is_measured_from_the_schedule_not_its_text(self):
        """`0,1 * * * *` and `*/1 * * * *` are the same rate, spelled differently.

        An earlier version compared the FIRST TWO occurrences and accepted
        `0,1 * * * *`: from a base landing on :00 those are :01 and the next
        hour's :00, which looks like 59 minutes. Sampling two points measures
        the base rather than the schedule.
        """
        with pytest.raises(ValueError, match=r"60s"):
            validate_schedule_cron("0,1 * * * *")
        assert MIN_SCHEDULE_INTERVAL_SECONDS == 300

    def test_a_real_zone_resolves_in_this_image(self):
        """`zoneinfo` reads the base image's tzdata, with no `tzdata` package.

        That is why there is no dependency for it -- and why this test exists:
        a base image that stops shipping tzdata would otherwise surface as an
        operator's schedule running in the wrong hour.
        """
        assert validate_schedule_timezone("Europe/London") == "Europe/London"
        assert validate_schedule_timezone("America/New_York") == "America/New_York"
        # ABSENT takes the default; an EXPLICIT empty round-trips rather than
        # becoming "UTC". Collapsing the two is a transform of a value a
        # Terraform provider sent, and "" planned against "UTC" read back is
        # "Provider produced inconsistent result after apply".
        assert validate_schedule_timezone(None) == "UTC"
        assert validate_schedule_timezone("") == ""
        with pytest.raises(ValueError):
            validate_schedule_timezone("Mars/Olympus")
