"""Configure runs on a calendar schedule (#2538).

This reverses the 2026-10-22 decision that Terrapod deliberately has no
scheduled configure, and the reasoning is worth keeping where the code is. The
original objection was to "a second scheduling vocabulary beside drift
detection's" -- an argument about design surface. What beat it was a migration
one: AWX schedules job templates as a first-class, heavily-used feature, so a
team moving off it arrives with schedules they depend on, and "stand up your own
cron that calls our API" is a materially worse answer for them than it is for a
greenfield user. It also asks them to operate something whose failure is silent,
since a timer that stops firing looks exactly like a workspace with nothing to
do.

**Cron rather than an interval, and that IS the second vocabulary.** An interval
cannot express "every Tuesday at 02:00": it drifts against the wall clock and is
anchored to whenever it was enabled. AWX schedules are calendar-anchored, so an
interval here would be the "thin version that misses the one property a team
needs" the issue warns about -- support in name only. The cost was accepted
knowingly.

**Drift detection is NOT moved to cron with it.** The two answer different
questions: drift asks "how stale may my view of reality be", which is a
duration, and a schedule asks "when do I want this to happen", which is a
calendar. Moving drift would be churn in service of a symmetry nobody asked for.

Everything else is borrowed rather than invented. The due check is a stored
timestamp plus arithmetic, exactly as drift's is; the de-duplication is
`inventory_auto_configure.configure_in_flight`, shared rather than restated; the
bounds are `default_playbook` against `default_limit`, which is already what a
configure nobody typed runs under.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, Workspace, now_utc
from terrapod.db.session import get_db_session
from terrapod.services import inventory_auto_configure, run_service, workspace_settings

logger = structlog.get_logger(__name__)

#: How often the cycle looks. A minute, because cron's own resolution is a
#: minute -- ticking slower would make `0 2 * * *` fire at some point after
#: 02:00 rather than at it, and ticking faster would buy nothing a cron
#: expression can express.
CYCLE_INTERVAL_SECONDS = 60

#: Why a schedule did not fire, for the log. Not an exception: every one of
#: these is a workspace behaving exactly as it declares, and a creator the
#: PLATFORM decided on has nobody to raise a 4xx to -- the reasoning
#: `inventory_auto_configure` already records for its own skips.
_SKIP_LOCKED = "workspace locked"
_SKIP_IN_FLIGHT = "a configure is already on its way"


def next_occurrence(cron: str, timezone: str, after: datetime) -> datetime | None:
    """The first firing of `cron` strictly after `after`, in UTC.

    `after` is read in the workspace's own zone and the answer converted back,
    so "02:00 every day" means 02:00 THERE across a DST change rather than a
    fixed UTC offset chosen when the schedule was written. That is the whole
    point of carrying a zone; without it the feature is an interval wearing a
    cron expression.
    """
    from croniter import croniter

    if not cron:
        return None
    try:
        zone = ZoneInfo(timezone or "UTC")
    except Exception:  # noqa: BLE001 - validated on write; a bad stored value
        zone = ZoneInfo("UTC")  # must not wedge the cycle for every workspace
    try:
        local = after.astimezone(zone)
        return croniter(cron, local).get_next(datetime).astimezone(ZoneInfo("UTC"))
    except Exception:  # noqa: BLE001
        return None


def is_due(ws: Workspace, now: datetime) -> bool:
    """Whether this workspace's schedule has come round.

    Measured from when it LAST FIRED, not from a wall-clock anchor, so a
    deployment that was down over an occurrence fires ONCE when it comes back
    rather than catching up on every missed one. That is drift detection's
    behaviour too, and it is the right one here for a stronger reason: a
    configure can run for an hour, so a catch-up storm would be a fleet-wide
    event nobody asked for.
    """
    since = ws.scheduled_configure_last_at or ws.created_at
    if since is None:
        return False
    nxt = next_occurrence(ws.scheduled_configure_cron, ws.scheduled_configure_timezone, since)
    return nxt is not None and nxt <= now


async def _create(db: AsyncSession, ws: Workspace) -> Run | None:
    try:
        run = await run_service.create_run(
            db,
            workspace=ws,
            message="Scheduled configure",
            source="schedule",
            is_configure=True,
        )
        # `create_run` leaves the run `pending` and does NOT queue it. Without
        # this the feature is inert AND silently self-blocking: the run sits at
        # `pending` for ever, and because `CONFIGURE_IN_FLIGHT` counts `pending`
        # every later cycle sees a configure "already on its way" and skips. One
        # dead run per workspace, then nothing. Recorded by the sibling creator
        # that learned it first.
        return await run_service.queue_run(db, run)
    except (
        run_service.PlanApplyNotEnabled,
        workspace_settings.ConfigureNotEnabled,
        workspace_settings.PlaybookNotAllowed,
        workspace_settings.WorkspaceLocked,
    ):
        # The platform decided on this run, so a refusal is skipped rather than
        # raised: there is nobody to tell, and a workspace behaving exactly as
        # it declares is not an error. DEBUG, so one stuck workspace is not a
        # warning per minute for ever.
        logger.debug(
            "Skipping scheduled configure: the workspace does not permit it",
            workspace=ws.name,
        )
        return None


async def scheduled_configure_cycle() -> None:
    """One pass over the workspaces carrying a schedule.

    Registered unconditionally with the distributed scheduler, so exactly one
    replica runs it per interval. There is no config flag gating registration --
    the newer convention, because a key whose only job is to skip one indexed
    SELECT is not worth the three legs of the config-channel contract.
    """
    now = now_utc()
    async with get_db_session() as db:
        result = await db.execute(
            select(Workspace).where(
                Workspace.scheduled_configure_enabled.is_(True),
                Workspace.scheduled_configure_cron != "",
                # A workspace that declares no configure operation cannot run
                # one. `create_run` would refuse it anyway; this is about not
                # logging a refusal once a minute for ever.
                Workspace.configure_enabled.is_(True),
            )
        )
        workspaces = list(result.scalars().all())
        if not workspaces:
            return

        for ws in workspaces:
            try:
                if not is_due(ws, now):
                    continue
                if ws.locked:
                    logger.debug(
                        "Skipping scheduled configure", reason=_SKIP_LOCKED, workspace=ws.name
                    )
                    continue
                if await inventory_auto_configure.configure_in_flight(db, ws.id):
                    # The stamp is NOT advanced here, deliberately. A schedule
                    # that collided with a long configure should fire at its next
                    # occurrence, not be treated as having fired now.
                    logger.debug(
                        "Skipping scheduled configure", reason=_SKIP_IN_FLIGHT, workspace=ws.name
                    )
                    continue

                run = await _create(db, ws)
                if run is not None:
                    # `now`, not the occurrence that came due. That is what makes
                    # a missed window fire once rather than catching up.
                    ws.scheduled_configure_last_at = now
                    await db.commit()
                    logger.info(
                        "Scheduled configure queued",
                        workspace=ws.name,
                        run_id=str(run.id),
                        cron=ws.scheduled_configure_cron,
                        timezone=ws.scheduled_configure_timezone,
                    )
            except Exception:  # noqa: BLE001
                # One workspace per cycle, never the cycle. The same shape as
                # the VCS pollers': a workspace whose schedule cannot be
                # evaluated must not stop every other workspace's.
                logger.exception("Scheduled configure failed", workspace=ws.name)
