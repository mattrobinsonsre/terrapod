"""Local-mode Pulumi state: one state version per update (#1564).

A `pulumi` CLI logged in to Terrapod checkpoints the stack many times during an
update. Each of those used to become a state version, so one long update
flooded the history with intermediate states nobody would roll back to.

Now a checkpoint is **held** against its update, overwriting the one before it,
and the last one is **promoted** to a single state version when the update
ends. "Ends" covers every way an update can end:

- `complete`, whatever status the CLI reports;
- `pulumi cancel`;
- the sweep, for an update whose CLI died and stopped renewing its lease.

A failed or abandoned update keeps its last checkpoint for the same reason a
failed Terraform apply keeps its partial state: it is the only record of what
the update created. That is the shape agent runs have had since #1576 — one
state version per state-changing update — and it is why local mode now matches.

The held checkpoint lives outside `state/{workspace_id}/` on purpose. Restore
treats every object under that prefix as a state version, and a checkpoint is
not one until it is promoted.

A promoted version carries what a Terraform state version carries: size, md5,
sha256 and its creator. Its size being non-zero is what lets the delete guard
in `state_management` protect the current version.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import StateVersion, Workspace, generate_uuid7
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


def _encode(deployment: Any) -> tuple[bytes, str, str]:
    """The stored bytes of a deployment, with their md5 and sha256 (worker thread)."""
    payload = json.dumps(deployment).encode()
    md5 = hashlib.md5(payload).hexdigest()  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5
    return payload, md5, hashlib.sha256(payload).hexdigest()


async def write_deployment(
    db: AsyncSession, ws: Workspace, deployment: Any, *, created_by: str | None
) -> StateVersion:
    """Store a deployment as the stack's next state version, and commit.

    Used for a promoted checkpoint and for `pulumi stack import`. The digests
    and size are over the stored plaintext, as they are for Terraform state.
    """
    from terrapod.crypto.state import encrypt_state_bytes
    from terrapod.storage import get_storage
    from terrapod.storage.keys import state_key

    latest = (
        await db.execute(
            select(StateVersion)
            .where(StateVersion.workspace_id == ws.id)
            .order_by(StateVersion.serial.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    serial = (latest.serial + 1) if latest else 1

    payload, md5, sha256 = await asyncio.to_thread(_encode, deployment)
    # Set here rather than left to the flush: the object is stored under this id,
    # and a restore numbers Pulumi versions by it, so it must be time-ordered.
    sv = StateVersion(
        id=generate_uuid7(),
        workspace_id=ws.id,
        serial=serial,
        md5=md5,
        sha256=sha256,
        state_size=len(payload),
        created_by=created_by,
    )
    db.add(sv)
    await db.flush()

    await get_storage().put(state_key(str(ws.id), str(sv.id)), await encrypt_state_bytes(payload))

    # State moved underneath any plan already made against this workspace, so
    # those plans are stale (#647). Every site that writes a state version owes
    # this call, and a guard test enforces it.
    from terrapod.services.run_service import discard_stale_plans_for_state_change

    await discard_stale_plans_for_state_change(db, ws.id, serial)
    await db.commit()

    # The break-glass index names every workspace's latest state (#1581).
    from terrapod.services import state_index_service

    await state_index_service.record_latest_state(
        workspace_name=ws.name, workspace_id=ws.id, state_version_id=sv.id, serial=serial
    )
    return sv


async def hold_checkpoint(
    workspace_id: uuid.UUID, update_id: str, deployment: Any, *, created_by: str | None
) -> None:
    """Keep a checkpoint against its update, replacing the one before it."""
    from terrapod.crypto.state import encrypt_state_bytes
    from terrapod.storage import get_storage
    from terrapod.storage.keys import pulumi_checkpoint_key

    envelope = {"created_by": created_by, "deployment": deployment}
    payload = await asyncio.to_thread(lambda: json.dumps(envelope).encode())
    await get_storage().put(
        pulumi_checkpoint_key(str(workspace_id), update_id), await encrypt_state_bytes(payload)
    )


async def promote_checkpoint(
    db: AsyncSession, ws: Workspace, update_id: str
) -> StateVersion | None:
    """Turn an update's last checkpoint into a state version.

    Returns the new version, or None when the update wrote no checkpoint — an
    update that changed nothing leaves no version behind. The held object is
    removed once the version is committed; failing to remove it costs a stray
    object, never a lost state.
    """
    from terrapod.crypto.state import decrypt_state_bytes
    from terrapod.storage import get_storage
    from terrapod.storage.keys import pulumi_checkpoint_key
    from terrapod.storage.protocol import ObjectNotFoundError

    storage = get_storage()
    key = pulumi_checkpoint_key(str(ws.id), update_id)
    try:
        raw = await storage.get(key)
    except ObjectNotFoundError:
        return None
    envelope = await asyncio.to_thread(json.loads, await decrypt_state_bytes(raw))
    sv = await write_deployment(
        db, ws, envelope.get("deployment"), created_by=envelope.get("created_by")
    )
    try:
        await storage.delete(key)
    except Exception:  # noqa: BLE001 — the version is already committed
        logger.warning("pulumi_checkpoint_not_removed", key=key)
    logger.info(
        "pulumi_checkpoint_promoted",
        stack=ws.name,
        update_id=update_id,
        serial=sv.serial,
        state_version_id=str(sv.id),
    )
    return sv
