"""Telling an operator that an artifact was refused (#2589, decision 2).

The decision this implements, recorded on the issue: *a verification failure or
an allowlist refusal is a real event with a meaning, and notification
configurations are the channel operators already have wired — so it joins them
rather than growing a parallel delivery path.*

**It is workspace-scoped because the refusal is, and that is measured rather
than assumed.** The package-cache routes authenticate with a RUNNER TOKEN
carrying `run_id`, so when a run's `pip install` is refused there is a run in
scope and therefore a workspace — which is the same shape every existing
trigger has, and why this needed no new delivery surface.

**A refusal with no run is logged and not delivered.** The pre-warm path
(`warm_ahead`) fetches on nobody's behalf, so there is no workspace whose
operators asked to hear about it. Inventing one — fanning out to every
workspace in the estate — would turn one refusal into N deliveries and teach
people to mute the channel. The log line is the record there.

**Never raises.** A refusal is already the bad news; failing to deliver news of
it must not also fail the fetch that was correctly refused.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

#: The trigger kind. Deliberately ONE kind covering both a failed signature and
#: an allowlist refusal: to an operator they are the same event -- something was
#: refused admission to what this deployment runs -- and splitting them would
#: make a complete subscription two boxes to tick instead of one.
TRIGGER = "artifact:refused"

#: Why it was refused, carried as a field rather than as separate triggers.
REASON_VERIFICATION = "verification-failed"
REASON_ALLOWLIST = "allowlist-refused"


async def notify_refusal(
    db,
    *,
    run_id: str | None,
    reason: str,
    coordinates: str,
    detail: str = "",
) -> None:
    """Enqueue an `artifact:refused` notification, if a workspace owns it.

    Takes the run and resolves the workspace here rather than asking every
    caller for both: the lookup only happens on a refusal, which is the rare
    path, and a caller that had to fetch the workspace first would pay for it
    on every successful fetch.
    """
    workspace_id = await _workspace_of(db, run_id) if run_id else None
    if not run_id or not workspace_id:
        # Not a gap: see the module docstring. A pre-warm refusal has no
        # workspace whose operators subscribed to it.
        logger.warning(
            "artifact refused with no run in scope, so no notification was delivered",
            reason=reason,
            coordinates=coordinates,
            detail=detail[:200],
        )
        return

    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "notification_deliver",
            {
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "trigger": TRIGGER,
                "reason": reason,
                "coordinates": coordinates,
                "detail": detail[:500],
            },
            # Keyed on the ARTIFACT as well as the run: one run resolving a
            # hundred dependencies can meet the same refused package several
            # times, and an operator wants to hear once.
            dedup_key=f"notif:{run_id}:{TRIGGER}:{coordinates}",
            dedup_ttl=300,
        )
    except Exception as exc:  # noqa: BLE001 - the refusal already happened
        logger.warning(
            "could not enqueue an artifact-refusal notification",
            reason=reason,
            coordinates=coordinates,
            error=str(exc),
        )


async def _workspace_of(db, run_id: str) -> str | None:
    """The workspace a run belongs to, or None if it cannot be resolved.

    Never raises: this runs while handling a refusal, and a lookup failure must
    not replace a clear "refused" with a database error.
    """
    try:
        from terrapod.db.models import Run

        run = await db.get(Run, run_id)
    except Exception:  # noqa: BLE001 - the refusal is the news, not this
        return None
    return str(run.workspace_id) if run is not None else None
