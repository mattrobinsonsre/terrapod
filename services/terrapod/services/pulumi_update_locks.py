"""A local Pulumi update holds the workspace lock (#1562).

A `pulumi` CLI logged in to Terrapod runs its update on the operator's machine
and writes checkpoints to the service surface as it goes. For the rest of
Terrapod that is exactly a Terraform CLI apply in local mode, so it takes the
same lock that one takes — the workspace's `locked` / `lock_id` — for as long as
the update runs:

- it shows as locked in the UI;
- the run dispatcher will not start an agent apply against the stack meanwhile,
  because it already refuses to on a locked workspace;
- a workspace that is already locked, manually or by another update, refuses the
  update and says who holds it;
- an update is refused while an agent run's apply is changing the stack.

Previews take no lock: they write no state, and taking one made concurrent
previews collide.

**An agent run's update does not take this lock either, although since #1881 it
drives the same surface.** It would refuse itself — the check is "a run on this
workspace is applying", and that run is the one asking — and it does not need
to: the dispatcher already permits one apply-capable run per workspace, and
`confirm_run` already refuses on a manual lock. What an agent update does take
is the Redis stack mutex, which is what stops a local `pulumi up` starting
alongside it. So the workspace lock stays what it is on the Terraform path: the
CLI/manual lock, never something run activity sets.

**Why a sweep.** The update's lease lives in Redis with a TTL, and an update
whose CLI dies simply stops renewing it. The workspace lock is a database row
and has no TTL, so something has to notice the lease is gone and release the
row. `sweep_abandoned_updates` is that something, run periodically by the
scheduler.

**Why more than a sweep.** The sweep infers death from a lapsed lease, which for
a local CLI is the best available signal — nothing else knows the process is
gone. An agent run is different: the listener reports its Job's outcome and the
reconciler acts on it, so Terrapod *knows* the run is over rather than inferring
it from silence. `handle_run_ended` ends that run's update at that moment
instead of leaving it to the next sweep cycle (#1882). The difference is
latency, not correctness — the sweep already promotes every abandoned
checkpoint — but the workspace lock is what holds the next apply back, so the
delay shows up as a stack that will not start.

The lock id names the update (`pulumi-update:<id>`), so releasing is always
conditional on the lock still being this update's. An operator can still clear
it with Terrapod's force-unlock, as for any lock left behind by a crashed CLI.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, Workspace
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

#: How long a lease is good for before the update is considered abandoned. The
#: CLI renews part-way through a long update, so this is the gap it may leave.
LEASE_TTL_SECONDS = 30 * 60

#: Every workspace lock a Pulumi update takes starts with this.
LOCK_ID_PREFIX = "pulumi-update:"

#: The triggered task that ends an agent run's update once the run is over.
RUN_ENDED_TRIGGER = "pulumi_run_ended"

#: Run statuses in which an agent apply is changing, or about to change, state.
_APPLYING = ("confirmed", "applying")


def update_key(update_id: str) -> str:
    """Redis key holding one update's record, lease included."""
    return f"tp:pulumi:update:{update_id}"


def stack_lock_key(workspace_id: str) -> str:
    """Redis key naming the update in flight on a stack.

    Set with NX, so it is what makes a second update's begin a 409 atomically;
    its TTL, renewed with the lease, is what lets a dead CLI's claim lapse.
    """
    return f"tp:pulumi:stack_active:{workspace_id}"


def lock_id_for(update_id: str) -> str:
    return f"{LOCK_ID_PREFIX}{update_id}"


def text_of(value: Any) -> str | None:
    """A Redis value as a string, whichever way the client returned it."""
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


class LockRefused(Exception):
    """The update may not take the workspace lock. `message` is shown verbatim."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


async def _publish(workspace_id: uuid.UUID, *, locked: bool) -> None:
    try:
        from terrapod.redis.client import publish_workspace_event

        await publish_workspace_event(
            str(workspace_id), "workspace_lock_change", {"locked": locked}
        )
    except Exception:  # noqa: BLE001 — a missed UI refresh must not fail the update
        logger.debug("Failed to publish workspace_lock_change", workspace_id=str(workspace_id))


def _lock_row(workspace_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """The workspace row, locked `FOR UPDATE OF workspaces`.

    **`of=` is load-bearing, and its absence was a live 500.** `Workspace` eagerly
    joins `vcs_connection` (`lazy="joined"`) on a nullable foreign key, so the ORM
    renders a LEFT OUTER JOIN — and Postgres refuses a bare `FOR UPDATE` over one:

        FOR UPDATE cannot be applied to the nullable side of an outer join

    Naming the entity locks only the `workspaces` row, which is the row these
    functions actually contend for, and leaves the join alone.

    This is the one place the lock row is read, so both the taker and the releaser
    are fixed by it and neither can drift back.
    """
    return select(Workspace).where(Workspace.id == workspace_id).with_for_update(of=Workspace)


async def take_workspace_lock(db: AsyncSession, workspace_id: uuid.UUID, update_id: str) -> None:
    """Lock the workspace for a local update, or raise `LockRefused` saying why.

    The workspace row is read `FOR UPDATE`, so a Terraform CLI lock or another
    update arriving at the same moment waits for this one to decide rather than
    both winning. The change goes through the ORM, not a Core UPDATE, because a
    Core statement never reaches the replication outbox and a standby would keep
    the old lock state (`test_replication_bulk_write_gate`).
    """
    applying = (
        await db.execute(
            select(Run.id)
            .where(
                Run.workspace_id == workspace_id,
                Run.status.in_(_APPLYING),
                Run.plan_only.is_(False),
            )
            .limit(1)
        )
    ).first()
    if applying is not None:
        raise LockRefused(
            "an agent run is applying to this stack; wait for it to finish, then run the "
            "update again"
        )

    ws = (await db.execute(_lock_row(workspace_id))).scalar_one_or_none()
    if ws is None:
        raise LockRefused("the stack's workspace no longer exists")
    if ws.locked:
        holder = ws.lock_id
        await db.rollback()
        raise LockRefused(
            f'the workspace is locked (lock ID: "{holder}"); unlock it in Terrapod, or wait '
            "for whatever holds it, then run the update again"
        )
    ws.locked = True
    ws.lock_id = lock_id_for(update_id)
    # A previous lock's note must not be reported as this one's (#1705).
    ws.lock_reason = "pulumi update"
    ws.locked_by = None
    await db.commit()
    await _publish(workspace_id, locked=True)
    logger.info(
        "pulumi_update_locked_workspace", workspace_id=str(workspace_id), update_id=update_id
    )


async def release_workspace_lock(db: AsyncSession, workspace_id: uuid.UUID, update_id: str) -> bool:
    """Release the workspace lock if — and only if — this update still holds it.

    Returns whether it did. A lock that has since been force-unlocked, or taken
    by something else, is left alone.
    """
    ws = (await db.execute(_lock_row(workspace_id))).scalar_one_or_none()
    if ws is None or ws.lock_id != lock_id_for(update_id):
        await db.rollback()
        return False
    ws.locked = False
    ws.lock_id = None
    ws.lock_reason = None
    ws.locked_by = None
    await db.commit()
    await _publish(workspace_id, locked=False)
    logger.info(
        "pulumi_update_released_workspace", workspace_id=str(workspace_id), update_id=update_id
    )
    return True


def decode_record(raw: dict | None) -> dict[str, str]:
    """A Redis hash as plain strings, whichever way the client returned it."""
    return {
        (k.decode() if isinstance(k, bytes) else str(k)): (
            v.decode() if isinstance(v, bytes) else str(v)
        )
        for k, v in (raw or {}).items()
    }


async def handle_run_ended(payload: dict) -> None:
    """End the update an agent run left behind, now that the run is over (#1882).

    Registered as a triggered task when the Pulumi engine is on, and enqueued
    from `run_service.transition_run` for every terminal state — cancelled,
    OOM-killed, node preempted, errored by the reconciler, and applied too,
    since a CLI that died just after its last checkpoint leaves exactly the same
    residue as one that was killed.

    **Which update is this run's.** The stack mutex names the update in flight
    on the stack, and the update's record says which run began it (`run_id`,
    written by `_begin_update` for a runner token). Both have to agree before
    anything is released: a local `pulumi up` may perfectly well hold this stack
    — it is refused only while an agent run is *applying*, so one that began
    before this run reached that point is legitimate — and releasing its lock
    would let a second update start alongside it. An update record that has
    already lapsed is left alone too: that is precisely the sweep's case, and it
    can promote what we no longer have the identity to claim.

    A preview is found by neither, because it takes no mutex and no lock. It
    leaves only its own record, which blocks nothing and expires on its own.

    **Ordering.** The last checkpoint becomes a state version *before* anything
    is released, for the reason the sweep gives: releasing first would leave the
    checkpoint held against an update nothing will ever look at again. So on any
    failure here nothing has been let go and the sweep, unchanged, is still the
    backstop — which is also what makes this safe to race against it. While the
    record still exists the sweep skips this update entirely; once we delete it
    the checkpoint object is already gone, so a sweep arriving afterwards
    promotes nothing and exactly one state version is written.
    """
    from terrapod.db.session import get_db_session
    from terrapod.redis.client import get_redis_client
    from terrapod.services.pulumi_checkpoint_service import promote_checkpoint

    run_id = payload.get("run_id")
    workspace_id = payload.get("workspace_id")
    if not run_id or not workspace_id:
        return

    redis = get_redis_client()
    update_id = text_of(await redis.get(stack_lock_key(workspace_id)))
    if not update_id:
        return
    record = decode_record(await redis.hgetall(update_key(update_id)))
    if not record or record.get("run_id") != str(run_id):
        return

    async with get_db_session() as db:
        ws = (
            await db.execute(select(Workspace).where(Workspace.id == uuid.UUID(workspace_id)))
        ).scalar_one_or_none()
        if ws is None:
            return
        try:
            await promote_checkpoint(db, ws, update_id)
        except Exception:  # noqa: BLE001 — the sweep retries; nothing is released yet
            await db.rollback()
            logger.warning(
                "pulumi_run_ended_checkpoint_not_promoted",
                workspace_id=workspace_id,
                update_id=update_id,
                run_id=run_id,
                exc_info=True,
            )
            return

        await redis.delete(update_key(update_id))
        # Release only if this update still holds it, exactly as `complete_update`
        # does: a lapsed lease may already have been replaced by a newer update,
        # and deleting that one's lock would let a third start alongside it.
        if text_of(await redis.get(stack_lock_key(workspace_id))) == update_id:
            await redis.delete(stack_lock_key(workspace_id))
        released = await release_workspace_lock(db, ws.id, update_id)

    logger.info(
        "pulumi_run_ended_update_released",
        workspace_id=workspace_id,
        update_id=update_id,
        run_id=run_id,
        kind=record.get("kind"),
        workspace_lock_released=released,
    )


async def sweep_abandoned_updates() -> int:
    """Release the workspace lock of every update whose lease has lapsed.

    Registered as a periodic task when the Pulumi engine is on. Returns how many
    locks it released.
    """
    from terrapod.db.session import get_db_session
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    released = 0
    from terrapod.services.pulumi_checkpoint_service import promote_checkpoint

    async with get_db_session() as db:
        held = (
            (
                await db.execute(
                    select(Workspace).where(Workspace.lock_id.like(f"{LOCK_ID_PREFIX}%"))
                )
            )
            .scalars()
            .all()
        )
        for ws in held:
            workspace_id = ws.id
            update_id = (ws.lock_id or "")[len(LOCK_ID_PREFIX) :]
            if await redis.exists(update_key(update_id)):
                continue
            # The update's last checkpoint is the only record of what it
            # created, so it becomes a state version before the stack is let go
            # (#1564). If that fails the lock stays and the next cycle tries
            # again: releasing it first would leave the checkpoint held against
            # an update nothing will ever look at again.
            try:
                await promote_checkpoint(db, ws, update_id)
            except Exception:  # noqa: BLE001 — one stack must not stop the sweep
                await db.rollback()
                logger.warning(
                    "pulumi_abandoned_update_checkpoint_not_promoted",
                    workspace_id=str(workspace_id),
                    update_id=update_id,
                    exc_info=True,
                )
                continue
            if await release_workspace_lock(db, workspace_id, update_id):
                released += 1
                if text_of(await redis.get(stack_lock_key(str(workspace_id)))) == update_id:
                    await redis.delete(stack_lock_key(str(workspace_id)))
                logger.info(
                    "pulumi_abandoned_update_released",
                    workspace_id=str(workspace_id),
                    update_id=update_id,
                )
    return released
