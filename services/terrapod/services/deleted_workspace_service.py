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

import asyncio
import hashlib
import json
import uuid as uuid_mod
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.crypto.state import decrypt_state_bytes, encrypt_state_bytes
from terrapod.db.models import StateVersion, Variable, Workspace
from terrapod.logging_config import get_logger
from terrapod.services.label_validation import sanitize_labels
from terrapod.services.workspace_name import validate_workspace_name
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    DELETED_MARKER_PREFIX,
    deleted_workspace_marker_key,
    state_key,
)
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
        # The boolean above is only a projection — it is true for `always`,
        # `create` and `create_update` alike. Recording the mode as well is what
        # makes a guardrail recoverable: without it an operator restoring a
        # `create_update` workspace knows auto-apply was on but not that it was
        # deliberately held back from changes and destroys, and the obvious way
        # to turn it back on is `always` (#1313).
        "auto_apply_mode": ws.auto_apply_mode,
        "auto_merge": ws.auto_merge,
        "auto_merge_strategy": ws.auto_merge_strategy,
        "plan_expiry_seconds": ws.plan_expiry_seconds,
        "security_scan_enforcement": ws.security_scan_enforcement,
        "security_scan_engine": ws.security_scan_engine,
        "security_scan_severity_threshold": ws.security_scan_severity_threshold,
        "security_scan_skip_rules": list(ws.security_scan_skip_rules or []),
        "ai_summary_mode": ws.ai_summary_mode,
        "ai_summary_context": ws.ai_summary_context,
        "slack_channel": ws.slack_channel,
        "trigger_prefixes": list(ws.trigger_prefixes or []),
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


# ---------------------------------------------------------------------------
# Restore (#1253 slice 2)
# ---------------------------------------------------------------------------
#
# Restore deliberately creates a **new workspace with a new id** and copies the
# state across, rather than re-attaching the original id to a fresh row.
#
# Re-attaching would be cheaper — the old prefix is already in the right place,
# so it would need no copy at all. It is not done, on purpose. Deleting a
# workspace should stay a consequential act; a one-keystroke undo that puts
# everything back exactly as it was invites casual deletion. Recovery here is a
# distinct, explicit, admin-only operation that produces a *new* workspace and
# says so — recoverable, but never free.
#
# The copy also leaves the original prefix and its marker untouched, so the
# retention window still governs the original and a restore can be repeated or
# reversed by deleting the restored copy.


def _state_object_keys(objects: list[Any], workspace_id: str) -> list[str]:
    """State-version blobs for a workspace, excluding backups.

    `state_backup_key` writes `{id}.backup.tfstate` into the same prefix; those
    are backups of a version, not versions, and restoring them would fabricate
    extra history.
    """
    prefix = f"state/{workspace_id}/"
    return sorted(
        o.key
        for o in objects
        if o.key.startswith(prefix)
        and o.key.endswith(".tfstate")
        and not o.key.endswith(".backup.tfstate")
    )


def _state_facts(plaintext: bytes) -> dict[str, Any]:
    """Recover a state version's metadata from the state document itself.

    This is the whole reason restore is not simply a row insert: deleting the
    workspace CASCADEd its `StateVersion` rows away, so serial and lineage no
    longer exist anywhere except inside the blobs. Reading them back is what
    makes the restored workspace continue the original state rather than start
    a new one — a fresh lineage would make the next apply fail on mismatch, or
    worse, treat live infrastructure as unmanaged.

    Runs in a worker thread (rule 13): both the parse and the digests are
    synchronous CPU over what can be a very large document.
    """
    doc = json.loads(plaintext)
    if not isinstance(doc, dict):
        raise ValueError("state document is not an object")
    serial = doc.get("serial")
    if not isinstance(serial, int):
        raise ValueError(f"state document has no usable serial: {serial!r}")
    return {
        "serial": serial,
        "lineage": str(doc.get("lineage") or ""),
        "md5": hashlib.md5(plaintext).hexdigest(),  # noqa: S324 — TFE protocol field
        "sha256": hashlib.sha256(plaintext).hexdigest(),
        "size": len(plaintext),
    }


async def _unique_name(db: AsyncSession, wanted: str) -> str:
    """A free, VALID workspace name, suffixed if `wanted` is taken.

    The original name is very often free — TFE semantics release it the moment
    the workspace is deleted, which is exactly why the row is not soft-deleted.
    But it may have been reused since, and a restore must never fail for a
    reason the operator cannot fix from the UI.

    `wanted` has two sources and only one is a request body: it also arrives
    from `marker["workspace_name"]`, read out of a JSON object in the bucket
    rather than from the database. Both used to be trusted unchecked, so a
    restore could mint a workspace whose name violates the format contract —
    and the name is load-bearing (the key `cloud {}` matches on, the `/app`
    redirect, the DR state index, VCS status contexts) (#1299).

    This end SANITIZES rather than raises, following the same split as
    labels: `validate_labels` rejects interactive input, `sanitize_labels`
    strips-and-logs for a flow that must not abort. A hand-edited or corrupt
    marker should not make an otherwise-recoverable workspace permanently
    un-restorable — the operator can rename afterwards. The router validates
    the *supplied* name strictly, because that one a human can just retype.
    """
    try:
        base = validate_workspace_name(wanted)[:80]
    except ValueError as e:
        logger.warning(
            "Restore name unusable — falling back",
            wanted=wanted,
            reason=str(e),
        )
        base = "restored-workspace"
    taken = await db.execute(select(Workspace.id).where(Workspace.name == base).limit(1))
    if taken.scalar_one_or_none() is None:
        return base
    for n in range(2, 100):
        candidate = f"{base}-restored-{n}"[:90]
        exists = await db.execute(select(Workspace.id).where(Workspace.name == candidate).limit(1))
        if exists.scalar_one_or_none() is None:
            return candidate
    raise ValueError(f"could not find a free name based on {base!r}")


#: Upper bound on how many state versions one restore will copy when the
#: deployment's own `state_versions_keep` is disabled (0 = keep everything).
#: Without a bound, one request walks every version ever written through the
#: pod's heap — decrypt, re-encrypt, put, per document — inside a single open
#: transaction, and a workspace with a large state will exhaust the ingress
#: timeout long before it finishes (#1299). The client then sees a failure
#: while the server keeps copying.
DEFAULT_MAX_RESTORE_VERSIONS = 20


async def restore_workspace(
    db: AsyncSession,
    storage: ObjectStore,
    workspace_id: str,
    *,
    restored_by: str,
    name: str | None = None,
    max_versions: int | None = None,
) -> tuple[Workspace, dict[str, Any]]:
    """Recover a deleted workspace as a NEW workspace holding its state history.

    Returns the new workspace and a report of everything that was deliberately
    *not* carried over, so the caller can show the operator what to re-enable.

    Three constraints shape this, in order of how much damage getting them
    wrong would do:

    1. **Lineage and serial are preserved exactly.** They come from the state
       documents (see `_state_facts`). This is the one hard correctness
       requirement; everything else is recoverable by editing the workspace.
    2. **It comes back inert.** Auto-apply off, drift detection off, VCS not
       connected — regardless of what the snapshot says. A restored workspace
       that immediately applies against infrastructure which has drifted, or
       been partly torn down since the delete, is the worst thing this feature
       could do. Re-enabling is a deliberate act.
    3. **Dangling references are dropped, never re-attached.** The VCS
       connection recorded in the marker may since have been deleted and its
       id reused. Silently binding the restored workspace to whatever now holds
       that id could point it at an entirely different repository.
    """
    marker = await read_marker(storage, workspace_id)
    if marker is None:
        raise LookupError(f"no delete marker for workspace {workspace_id}")

    objects = await storage.list_prefix(f"state/{workspace_id}/")
    keys = _state_object_keys(objects, workspace_id)

    # Keys sort by the time-ordered uuid7 state-version id, so the newest
    # versions are at the end. When the cap bites, keep those: a restore is
    # about resuming from where the workspace left off, and the current serial
    # is the one the next plan reads.
    cap = max_versions if max_versions and max_versions > 0 else DEFAULT_MAX_RESTORE_VERSIONS
    beyond_cap: list[str] = []
    if len(keys) > cap:
        beyond_cap = keys[:-cap]
        keys = keys[-cap:]

    settings = marker.get("settings") or {}
    report: dict[str, Any] = {
        "source_workspace_id": workspace_id,
        "source_workspace_name": marker.get("workspace_name"),
        "state_versions_restored": 0,
        "state_versions_skipped": [
            {"key": k, "reason": f"beyond the {cap}-version restore cap"} for k in beyond_cap
        ],
        "suppressed": [],
        "dropped_references": [],
    }

    # Labels went through validation on the way in, but a marker is a file in a
    # bucket: it may predate a key becoming reserved, or have been edited. Strip
    # rather than reject — refusing to restore because of a label would be a
    # poor trade.
    labels, stripped = sanitize_labels(settings.get("labels"))
    if stripped:
        report["dropped_references"].append({"field": "labels", "keys": stripped})

    ws = Workspace(
        id=uuid_mod.uuid4(),
        name=await _unique_name(db, name or marker.get("workspace_name") or ""),
        labels=labels,
        owner_email=settings.get("owner_email") or restored_by,
        execution_mode=settings.get("execution_mode") or "local",
        execution_backend=settings.get("execution_backend") or "tofu",
        terraform_version=settings.get("terraform_version") or "1.12",
        terragrunt_enabled=bool(settings.get("terragrunt_enabled")),
        terragrunt_version=settings.get("terragrunt_version") or "1.0",
        working_directory=settings.get("working_directory") or "",
        var_files=list(settings.get("var_files") or []),
        resource_cpu=settings.get("resource_cpu") or "1",
        resource_memory=settings.get("resource_memory") or "2Gi",
        drift_ignore_rules=list(settings.get("drift_ignore_rules") or []),
        # Settings that only describe how a run is evaluated, and cannot start
        # one, come back as they were. Restoring a workspace with its security
        # scanning silently back at the defaults would be its own quiet
        # downgrade (#1313).
        plan_expiry_seconds=settings.get("plan_expiry_seconds"),
        security_scan_enforcement=settings.get("security_scan_enforcement") or "advisory",
        security_scan_engine=settings.get("security_scan_engine") or "checkov",
        security_scan_severity_threshold=(
            settings.get("security_scan_severity_threshold") or "high"
        ),
        security_scan_skip_rules=list(settings.get("security_scan_skip_rules") or []),
        ai_summary_mode=settings.get("ai_summary_mode") or "default",
        ai_summary_context=settings.get("ai_summary_context") or "",
        slack_channel=settings.get("slack_channel") or "",
        trigger_prefixes=list(settings.get("trigger_prefixes") or []),
        auto_merge_strategy=settings.get("auto_merge_strategy") or "merge",
        # Constraint 2 — inert on return. Anything that can act on its own is
        # off, whatever it was: auto-apply (in both columns, so the boolean and
        # the mode cannot disagree), drift detection, and auto-merge, which
        # writes to a VCS provider.
        auto_apply=False,
        auto_apply_mode="never",
        drift_detection_enabled=False,
        auto_merge=False,
    )
    if settings.get("auto_apply"):
        # Plain field names, so the list stays machine-readable. What it *was*
        # — which matters, because "auto_apply" alone would send an operator
        # restoring a `create_update` workspace to `always` — is on the
        # marker's `settings`, which the undelete surface returns (#1313).
        report["suppressed"].append("auto_apply")
    if settings.get("drift_detection_enabled"):
        report["suppressed"].append("drift_detection_enabled")
    if settings.get("auto_merge"):
        report["suppressed"].append("auto_merge")
    # Constraint 3 — VCS is described, never re-attached.
    if settings.get("vcs_connection_id") or settings.get("vcs_repo_url"):
        report["dropped_references"].append(
            {
                "field": "vcs",
                "connection_id": settings.get("vcs_connection_id"),
                "repo_url": settings.get("vcs_repo_url"),
                "branch": settings.get("vcs_branch"),
            }
        )
        report["suppressed"].append("vcs_connection")

    db.add(ws)
    await db.flush()

    new_id = str(ws.id)
    seen_serials: set[int] = set()
    for key in keys:
        raw = await storage.get(key)
        try:
            plaintext = await decrypt_state_bytes(raw)
            facts = await asyncio.to_thread(_state_facts, plaintext)
        except Exception as e:  # noqa: BLE001 — one bad blob must not sink the restore
            logger.warning("Skipping unreadable state blob during restore", key=key, error=str(e))
            report["state_versions_skipped"].append({"key": key, "reason": str(e)})
            continue

        # `(workspace_id, serial)` is unique. Two blobs claiming one serial can
        # only be a duplicate write; keeping the first (keys sort by the
        # time-ordered uuid7 id) is arbitrary but stable, and inserting both
        # would abort the whole transaction.
        if facts["serial"] in seen_serials:
            report["state_versions_skipped"].append(
                {"key": key, "reason": f"duplicate serial {facts['serial']}"}
            )
            continue
        seen_serials.add(facts["serial"])

        sv = StateVersion(
            workspace_id=ws.id,
            serial=facts["serial"],
            lineage=facts["lineage"],
            md5=facts["md5"],
            sha256=facts["sha256"],
            state_size=facts["size"],
        )
        db.add(sv)
        await db.flush()

        # Re-encrypt rather than copying the source bytes: the source may have
        # been written under a DEK version that has since been rotated away,
        # and a restore is the right moment to bring it onto the active key.
        await storage.put(
            state_key(new_id, str(sv.id)),
            await encrypt_state_bytes(plaintext),
            content_type="application/json",
        )
        report["state_versions_restored"] += 1

    return ws, report


async def record_restore(
    storage: ObjectStore, workspace_id: str, *, new_workspace_id: str, restored_by: str
) -> None:
    """Stamp the marker with where this deletion was restored to.

    Nothing about restore is exclusive: the marker is not consumed, the source
    prefix is untouched, and `_unique_name` suffixes rather than conflicting —
    so a second restore quietly succeeds and yields a second live workspace
    with the SAME lineage and serial over the same real infrastructure. An
    apply in either then makes the other's next plan read as wholesale drift,
    and nothing in the API said it had happened (#1299).

    Stamping is what lets the restore endpoint refuse a repeat, and lets the
    list surface show one has already occurred. Best-effort by design — the
    restore itself has committed, and failing the request over a marker write
    would report a failure for a workspace that exists.
    """
    marker = await read_marker(storage, workspace_id)
    if marker is None:
        return
    history = marker.get("restored_to")
    marker["restored_to"] = (
        [*history, new_workspace_id] if isinstance(history, list) else [new_workspace_id]
    )
    marker["restored_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    marker["restored_by"] = restored_by
    await write_marker(storage, workspace_id, marker)


def prior_restores(marker: dict[str, Any]) -> list[str]:
    """Workspace ids this deletion has already been restored into."""
    history = marker.get("restored_to")
    return [str(i) for i in history] if isinstance(history, list) else []


def _restorable_until(marker: dict[str, Any], retention_days: int) -> str | None:
    """When this deletion stops being recoverable. `0 = disabled` (no expiry),
    matching the retention service."""
    if retention_days <= 0 or not isinstance(marker.get("deleted_at"), str):
        return None
    try:
        ts = datetime.fromisoformat(marker["deleted_at"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return (ts + timedelta(days=retention_days)).isoformat().replace("+00:00", "Z")


def _decorate(marker: dict[str, Any], retention_days: int) -> dict[str, Any]:
    """Marker plus the derived fields the undelete surface needs.

    Deliberately excludes `state_versions_available`, which costs a storage
    listing per marker — see `attach_state_counts`.
    """
    age = marker_age_days(marker)
    return {
        **marker,
        "age_days": round(age, 2) if age is not None else None,
        "restorable_until": _restorable_until(marker, retention_days),
        "restored_to": prior_restores(marker),
    }


async def attach_state_counts(
    storage: ObjectStore, markers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fill in `state_versions_available` for the markers given.

    Counted from storage rather than trusted from the marker: the marker's
    count was true at delete time, but a later reap or a partial replication is
    exactly what an operator needs to see before deciding whether a restore is
    worth attempting.

    Separated from `list_deleted` because it costs one full prefix listing per
    marker, and the list endpoint used to pay that for **every** deleted
    workspace ever, before pagination had a chance to narrow it (#1299). The
    caller pages first and decorates only what it is about to return.
    """
    for m in markers:
        ws_id = m.get("workspace_id") or ""
        objects = await storage.list_prefix(f"state/{ws_id}/")
        m["state_versions_available"] = len(_state_object_keys(objects, ws_id))
    return markers


async def get_deleted(
    db: AsyncSession, storage: ObjectStore, workspace_id: str, retention_days: int
) -> dict[str, Any] | None:
    """One deleted workspace, read directly by id.

    Reads the single marker rather than building the whole list and scanning it
    for a match — which is what this did, making a request for one workspace
    cost a listing of every marker plus a state listing for each (#1299).
    """
    try:
        uuid_mod.UUID(workspace_id)
    except ValueError:
        return None
    marker = await read_marker(storage, workspace_id)
    if marker is None:
        return None
    # A live id is not a deleted workspace, however stale the marker is.
    live = await db.execute(select(Workspace.id).where(Workspace.id == workspace_id).limit(1))
    if live.scalar_one_or_none() is not None:
        return None
    marker["workspace_id"] = workspace_id
    decorated = _decorate(marker, retention_days)
    await attach_state_counts(storage, [decorated])
    return decorated


async def list_deleted(
    db: AsyncSession, storage: ObjectStore, retention_days: int
) -> list[dict[str, Any]]:
    """Every deleted workspace still recoverable, newest deletion first.

    Driven by the markers, not by a scan of state prefixes: a marker is the
    thing that says a workspace *was* deleted, and the reaper writes one for
    any orphan it finds without. A prefix with no marker yet is not omitted
    from the world, it is simply not yet stamped — it will appear after the
    next retention cycle.

    A workspace whose id is live again is filtered out rather than shown as
    deleted. That happens when a marker write raced a recreate, or when a
    restore reused the name; either way the operator should not be offered a
    recovery for something that exists.
    """
    markers: list[dict[str, Any]] = []
    for obj in await storage.list_prefix(DELETED_MARKER_PREFIX):
        if not obj.key.endswith(".json"):
            continue
        ws_id = obj.key[len(DELETED_MARKER_PREFIX) : -len(".json")]
        try:
            uuid_mod.UUID(ws_id)
        except ValueError:
            logger.warning("Ignoring non-UUID delete marker", key=obj.key)
            continue
        body = await read_marker(storage, ws_id)
        if body is None:
            continue
        # The KEY is the authority for the id, not the body. The key was just
        # UUID-validated; the body is a hand-editable JSON file, and a
        # non-UUID `workspace_id` in one used to reach the `Workspace.id.in_()`
        # below and 500 the whole endpoint for every admin — one bad object
        # taking out the entire undelete surface (#1299).
        body["workspace_id"] = ws_id
        markers.append(body)

    if not markers:
        return []

    ids = [m["workspace_id"] for m in markers if m.get("workspace_id")]
    live = {
        str(w)
        for w in (await db.execute(select(Workspace.id).where(Workspace.id.in_(ids)))).scalars()
    }

    out = [
        _decorate(m, retention_days) for m in markers if (m.get("workspace_id") or "") not in live
    ]
    out.sort(key=lambda m: m.get("deleted_at") or "", reverse=True)
    return out
