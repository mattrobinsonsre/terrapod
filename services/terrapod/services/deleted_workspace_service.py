"""Delete markers for workspace undelete (#1253).

Deleting a workspace removes its rows — `StateVersion` goes by CASCADE — but
**nothing removes the state blobs**. They stay at `state/{workspace_id}/…`
indefinitely. So the data survives a delete; what is destroyed is the index
saying which id had which name.

That has two consequences this module addresses together, because they are the
same fact seen from two sides:

  * a workspace deleted by mistake is recoverable in principle and unfindable
    in practice; and
  * `artifact_retention_service` reaps state by walking `StateVersion` **rows**,
    so once CASCADE removes them the blobs are permanently unreachable by the
    reaper — every workspace ever deleted still occupies storage, forever.

A marker written at delete time fixes both: it names the workspace, dates the
deletion, and gives the reaper something to age.

**The marker body is the contract, not its object metadata and not its
mtime.** `blob_sync._copy_one` streams a replicated object in with no
`metadata=` and the destination stamps a fresh `last_modified`, so anything
carried outside the body is lost or falsified on a standby — a marker dated by
mtime would read as "deleted just now" after every replication cycle and never
age out. The body is the one thing replication copies faithfully and
size-verifies.

**No secrets, ever.** This object lands in the bucket, replicates to a peer, and
is readable by anyone with storage access. Variable *values* never go in — only
their names, which is what an operator needs to know what to recreate. The VCS
connection is referenced by id; its credential is never copied here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import StateVersion, Variable, Workspace
from terrapod.logging_config import get_logger
from terrapod.storage import get_storage
from terrapod.storage.keys import deleted_workspace_marker_key
from terrapod.storage.protocol import ObjectNotFoundError, ObjectStore

logger = get_logger(__name__)

#: Bumped only on a breaking change to the body shape. A reader that does not
#: recognise the version still gets `deleted_at` and `workspace_name`, which
#: are the two fields the undelete list cannot work without — so keep those at
#: the top level of every future version.
MARKER_VERSION = 1

#: Written by the delete path, with a true deletion timestamp.
REASON_DELETED = "deleted"
#: Written by the reaper on first sight of an orphan it has no marker for —
#: a workspace deleted before this shipped, or one whose marker write failed.
#: `deleted_at` is then the discovery time, not the real deletion time, which
#: is why the two reasons are distinguishable.
REASON_DISCOVERED = "discovered-orphaned"


def _settings_snapshot(ws: Workspace) -> dict[str, Any]:
    """The workspace's own configuration, as it was at delete time.

    Restoring these is what makes an undelete useful rather than merely a state
    recovery — labels and ownership especially, since those are what a
    workspace is found by afterwards.

    Every field here is non-secret by construction. Nothing that holds or
    references a credential value belongs in this dict; `vcs_connection_id` is
    a pointer to a connection whose token stays in the database.
    """
    return {
        "labels": dict(ws.labels or {}),
        "owner_email": ws.owner_email,
        "execution_mode": ws.execution_mode,
        "execution_backend": ws.execution_backend,
        "terraform_version": ws.terraform_version,
        "terragrunt_enabled": ws.terragrunt_enabled,
        "terragrunt_version": ws.terragrunt_version,
        "working_directory": ws.working_directory,
        "var_files": list(ws.var_files or []),
        "resource_cpu": ws.resource_cpu,
        "resource_memory": ws.resource_memory,
        "auto_apply": ws.auto_apply,
        "vcs_connection_id": str(ws.vcs_connection_id) if ws.vcs_connection_id else None,
        "vcs_repo_url": ws.vcs_repo_url,
        "vcs_branch": ws.vcs_branch,
        "vcs_workflow": ws.vcs_workflow,
        "drift_detection_enabled": ws.drift_detection_enabled,
        "drift_detection_interval_seconds": ws.drift_detection_interval_seconds,
        "drift_ignore_rules": list(ws.drift_ignore_rules or []),
    }


async def build_marker(db: AsyncSession, ws: Workspace, deleted_by: str) -> dict[str, Any]:
    """Assemble the marker body for a workspace about to be deleted."""
    latest = (
        await db.execute(
            select(StateVersion)
            .where(StateVersion.workspace_id == ws.id)
            .order_by(StateVersion.serial.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    count = len(
        (await db.execute(select(StateVersion.id).where(StateVersion.workspace_id == ws.id)))
        .scalars()
        .all()
    )

    # Names only — never values. A sensitive variable's value is encrypted at
    # rest and must not be decrypted into a plaintext object in the bucket.
    variables = (
        (await db.execute(select(Variable).where(Variable.workspace_id == ws.id))).scalars().all()
    )

    return {
        "marker_version": MARKER_VERSION,
        "marker_reason": REASON_DELETED,
        "workspace_id": str(ws.id),
        "workspace_name": ws.name,
        "deleted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "deleted_by": deleted_by,
        "last_serial": latest.serial if latest else None,
        "lineage": latest.lineage if latest else None,
        "state_version_count": count,
        "settings": _settings_snapshot(ws),
        "variable_names": [
            {"key": v.key, "category": v.category, "sensitive": v.sensitive} for v in variables
        ],
    }


def build_discovery_marker(
    workspace_id: str, observed_at: datetime | None = None
) -> dict[str, Any]:
    """Marker for an orphan the reaper found with no marker of its own.

    The retention clock has to start somewhere, and the only defensible choice
    is when we first saw it: the newest blob's mtime is the last *state write*,
    which for a workspace last applied months before it was deleted would make
    it instantly reapable and give no undelete window at all.

    Starting at discovery means a pre-existing orphan gets the full window from
    the moment this feature ships, rather than being retroactively expired.
    """
    ts = observed_at or datetime.now(UTC)
    return {
        "marker_version": MARKER_VERSION,
        "marker_reason": REASON_DISCOVERED,
        "workspace_id": workspace_id,
        "workspace_name": None,
        "deleted_at": ts.isoformat().replace("+00:00", "Z"),
        "deleted_by": None,
        "last_serial": None,
        "lineage": None,
        "settings": {},
        "variable_names": [],
    }


async def write_marker_best_effort(workspace_id: str, body: dict[str, Any]) -> None:
    """Write a marker without letting any failure reach the caller.

    The delete has already committed by the time this runs, so raising here
    would return a 500 for a workspace that *is* gone — the operator retries,
    gets a 404, and is left believing the delete failed. Resolving the store is
    inside the guard too: an unconfigured or unavailable store must degrade to
    "no marker" (the reaper stamps it on a later cycle), never to a failed
    delete.
    """
    try:
        await write_marker(get_storage(), workspace_id, body)
    except Exception as e:  # noqa: BLE001 — a half-failed delete is worse
        logger.warning(
            "Could not write workspace delete marker",
            workspace_id=workspace_id,
            error=str(e),
        )


async def write_marker(storage: ObjectStore, workspace_id: str, body: dict[str, Any]) -> None:
    """Persist a marker. Best-effort: a failure must not fail the delete."""
    try:
        await storage.put(
            deleted_workspace_marker_key(workspace_id),
            json.dumps(body, indent=2).encode(),
            content_type="application/json",
        )
    except Exception as e:  # noqa: BLE001 — a delete that half-fails is worse
        logger.warning(
            "Failed to write workspace delete marker — the workspace is deleted but "
            "will show as an undated orphan until the reaper stamps it",
            workspace_id=workspace_id,
            error=str(e),
        )


async def read_marker(storage: ObjectStore, workspace_id: str) -> dict[str, Any] | None:
    """Read a marker, or None if there isn't one (or it is unreadable)."""
    try:
        raw = await storage.get(deleted_workspace_marker_key(workspace_id))
    except ObjectNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Delete marker unreadable", workspace_id=workspace_id, error=str(e))
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as e:
        # A corrupt marker must not make an orphan immortal, but it must not
        # make it instantly reapable either — the caller treats None as
        # "unstamped" and writes a fresh discovery marker, restarting the clock.
        logger.warning("Delete marker is not valid JSON", workspace_id=workspace_id, error=str(e))
        return None
    return parsed if isinstance(parsed, dict) else None


def marker_age_days(body: dict[str, Any], now: datetime | None = None) -> float | None:
    """Age of a marker in days, or None if it carries no usable timestamp."""
    raw = body.get("deleted_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - ts).total_seconds() / 86400.0
