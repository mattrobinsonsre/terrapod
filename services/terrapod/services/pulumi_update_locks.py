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

**Why a sweep.** The update's lease lives in Redis with a TTL, and an update
whose CLI dies simply stops renewing it. The workspace lock is a database row
and has no TTL, so something has to notice the lease is gone and release the
row. `sweep_abandoned_updates` is that something, run periodically by the
scheduler.

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

    ws = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
    ).scalar_one_or_none()
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
    ws = (
        await db.execute(select(Workspace).where(Workspace.id == workspace_id).with_for_update())
    ).scalar_one_or_none()
    if ws is None or ws.lock_id != lock_id_for(update_id):
        await db.rollback()
        return False
    ws.locked = False
    ws.lock_id = None
    await db.commit()
    await _publish(workspace_id, locked=False)
    logger.info(
        "pulumi_update_released_workspace", workspace_id=str(workspace_id), update_id=update_id
    )
    return True


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
