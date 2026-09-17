"""Terrapod-specific state management endpoints.

These endpoints are NOT part of the TFE V2 API specification. They provide
state lifecycle operations consumed by the web UI: delete, rollback, and
manual upload.

Endpoints:
    DELETE /api/terrapod/v1/state-versions/{id}/manage — delete a non-current state version
    POST   /api/terrapod/v1/state-versions/{id}/actions/rollback — rollback to an older version
    POST   /api/terrapod/v1/workspaces/{id}/state-versions/actions/upload — manual state upload
"""

import asyncio
import hashlib
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.upload_stream import file_chunks, read_file_bytes, stream_to_tempfile
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import (
    resolve_workspace_capabilities_for,
)
from terrapod.storage import get_storage
from terrapod.storage.keys import state_key

router = APIRouter(tags=["state-management"])
logger = get_logger(__name__)


async def _get_state_version(state_version_id: str, db: AsyncSession) -> StateVersion:
    """Look up a state version by its sv-{uuid} ID."""
    sv_uuid = state_version_id.removeprefix("sv-")
    result = await db.execute(select(StateVersion).where(StateVersion.id == sv_uuid))
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="State version not found")
    return sv


def _read_state_lineage_md5(path: str) -> tuple[str, str]:
    """Read (lineage, md5) from a state file on disk (worker thread).

    Both the md5 hash and `json.load` would block the event loop on a
    multi-MB state if run inline (CLAUDE.md #13). The file lives on the
    ephemeral PVC, not the worker heap (#14).
    """
    h = hashlib.md5()  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    with open(path, "rb") as fh:
        state_data = json.load(fh)
    return state_data.get("lineage", ""), h.hexdigest()


def _read_pulumi_export(path: str) -> tuple[bytes, str, str]:
    """A Pulumi state upload as the deployment to store, with md5 and sha256 (worker thread).

    Accepts `pulumi stack export` output — `{"version": 3, "deployment": {...}}` —
    and a bare deployment, which is what `export` prints the deployment as.
    Anything else, a Terraform state included, is refused: stored, it would be
    read back as the stack's deployment.

    An export made with `--show-secrets` is refused too. Stored as it stands it
    would put the stack's secrets in the state in the clear; `pulumi stack import`
    is the way to load one, because the CLI seals them first.
    """
    from terrapod.services.pulumi_state_service import has_plaintext_secrets

    with open(path, "rb") as fh:
        doc = json.load(fh)
    deployment = doc.get("deployment") if isinstance(doc, dict) and "deployment" in doc else doc
    if not isinstance(deployment, dict) or "manifest" not in deployment:
        raise ValueError("expected the output of `pulumi stack export`")
    if has_plaintext_secrets(deployment):
        raise ValueError(
            "the export carries plaintext secrets; export without --show-secrets, "
            "or load it with `pulumi stack import`"
        )
    payload = json.dumps(deployment).encode()
    md5 = hashlib.md5(payload).hexdigest()  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5
    return payload, md5, hashlib.sha256(payload).hexdigest()


def _top_level_serial_span(buf: bytes) -> tuple[int, int] | None:
    """Byte offsets of the top-level `"serial"` value in a JSON object, or None.

    A small JSON scanner rather than a parse. It tracks string and nesting state
    so that a `serial` key inside a resource's attributes, or the text "serial"
    inside a string, is never mistaken for the state's own. It stops as soon as
    it finds the value, which terraform and tofu write in the first few lines.

    None means the value was not found in `buf`: either the object has no
    top-level serial, or `buf` is a prefix that ends before it.
    """
    depth = 0
    in_string = False
    string_is_key = False
    escaped = False
    key_start = -1
    last_key: bytes | None = None
    expecting_value = False
    i, n = 0, len(buf)
    while i < n:
        c = buf[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == 0x5C:  # backslash
                escaped = True
            elif c == 0x22:  # closing quote
                in_string = False
                if string_is_key:
                    last_key = buf[key_start:i]
            i += 1
            continue
        if c == 0x22:  # opening quote
            in_string = True
            key_start = i + 1
            # At the top level a string is a key unless a colon came first.
            string_is_key = depth == 1 and not expecting_value
            if depth == 1:
                expecting_value = False
        elif c in (0x7B, 0x5B):  # { [
            depth += 1
            expecting_value = False
        elif c in (0x7D, 0x5D):  # } ]
            depth -= 1
            if depth <= 0:
                return None
        elif depth == 1 and c == 0x3A:  # colon after a top-level key
            if last_key == b"serial":
                j = i + 1
                while j < n and buf[j] in (0x20, 0x09, 0x0A, 0x0D):
                    j += 1
                k = j
                while k < n and (0x30 <= buf[k] <= 0x39 or buf[k] == 0x2D):
                    k += 1
                if k == n:
                    return None  # the value may continue past this prefix
                if k == j:
                    raise ValueError("state serial is not a number")
                return j, k
            expecting_value = True
        elif depth == 1 and c == 0x2C:  # comma between top-level members
            expecting_value = False
            last_key = None
        i += 1
    return None


def _state_with_serial(state_bytes: bytes, serial: int) -> bytes:
    """The state file with its top-level `serial` set to `serial`.

    A state version has two serials: the row's, which the upload handler checks
    for collisions, and the one inside the file, which terraform/tofu read to
    compute the NEXT serial. They must be equal (#1702). A path that assigns a
    fresh row serial and stores the file verbatim breaks that: the engine counts
    on from the file's older serial, lands on a row that already exists, and the
    upload is refused with 409 -- after the apply has changed infrastructure,
    and again on every later run.

    Only the digits of that one value change; every other byte is kept. That is
    required, not tidiness. The engine serializes state its own way (Go escapes
    `<`, `>` and `&`, and writes UTF-8 unescaped), and a later apply that
    changes nothing re-uploads the engine's bytes at this serial. The runner
    upload treats that as a no-op only if the bytes are identical; re-encoding
    the state here would make it a 409 and mark the workspace diverged.
    """
    stripped = state_bytes.lstrip()
    if not stripped.startswith(b"{"):
        raise ValueError("state is not a JSON object")
    span = _top_level_serial_span(state_bytes)
    if span is None:
        # No top-level serial. Nothing to align, and inventing one would mean
        # re-encoding the state; store it as it came.
        return state_bytes
    a, b = span
    return state_bytes[:a] + str(serial).encode() + state_bytes[b:]


def _rewrite_state_file_serial(path: str, serial: int) -> tuple[str, str, int]:
    """Set the top-level `serial` in a state file on disk; return (md5, sha256, size).

    The upload streams to a PVC tempfile (#14). Only the head of the file is read
    to find the serial -- terraform and tofu write it in the first few lines --
    and the rest is copied through, so the state is never held in memory.
    """
    import shutil

    window = 64 * 1024
    with open(path, "rb") as fh:
        head = fh.read(window)
        if not head.lstrip().startswith(b"{"):
            raise ValueError("state is not a JSON object")
        span = _top_level_serial_span(head)
        while span is None:
            more = fh.read(window)
            if not more:
                break
            head += more
            span = _top_level_serial_span(head)
            window *= 2

    md5 = hashlib.md5()  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5
    sha = hashlib.sha256()

    if span is None:
        size = 0
        with open(path, "rb") as fh:
            while chunk := fh.read(1024 * 1024):
                md5.update(chunk)
                sha.update(chunk)
                size += len(chunk)
        return md5.hexdigest(), sha.hexdigest(), size

    a, b = span
    prefix = head[:a] + str(serial).encode()
    tmp = path + ".serial"
    size = 0
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(prefix)
        md5.update(prefix)
        sha.update(prefix)
        size += len(prefix)
        src.seek(b)
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
            md5.update(chunk)
            sha.update(chunk)
            size += len(chunk)
    shutil.move(tmp, path)
    return md5.hexdigest(), sha.hexdigest(), size


async def _require_sv_workspace_capability(
    sv: StateVersion,
    required: str,
    user: AuthenticatedUser,
    db: AsyncSession,
) -> Workspace:
    """Check a capability on the state version's workspace. Returns workspace."""
    ws = await db.get(Workspace, sv.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} capability on workspace",
        )
    return ws


@router.delete("/state-versions/{state_version_id}/manage")
async def delete_state_version(
    state_version_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a non-current state version. Requires admin permission.

    The current (highest serial) state version cannot be deleted — this
    protects against accidentally removing the active workspace state.
    """
    sv = await _get_state_version(state_version_id, db)
    ws = await _require_sv_workspace_capability(sv, cap.STATE_DELETE, user, db)

    # Prevent deleting the current (latest) state version, UNLESS the
    # record is an unuploaded orphan placeholder. We can't gate on
    # md5 (it's set at create_state_version time from the client-
    # declared value, so it's already non-empty for an orphan) — we
    # gate on state_size, which upload_state_content sets atomically
    # with the actual bytes. state_size == 0 ⇔ /content PUT never
    # succeeded ⇔ no real terraform data exists for this row.
    #
    # Threat: an admin could create a placeholder at serial N+1 and
    # then delete the real previous-current at serial N. That's not
    # a new attack — admin can already delete non-current state
    # versions, and creating a placeholder + deleting old + deleting
    # placeholder strings together capabilities admin already has
    # directly. Logged in audit_log.
    max_serial_result = await db.execute(
        select(func.max(StateVersion.serial)).where(StateVersion.workspace_id == sv.workspace_id)
    )
    max_serial = max_serial_result.scalar_one_or_none()
    if max_serial is not None and sv.serial == max_serial and (sv.state_size or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete the current state version",
        )

    # Delete from object storage
    storage = get_storage()
    key = state_key(str(sv.workspace_id), str(sv.id))
    try:
        await storage.delete(key)
    except Exception:
        logger.warning(
            "state_version_storage_delete_failed",
            state_version_id=str(sv.id),
            key=key,
        )

    await db.delete(sv)
    await db.commit()

    logger.info(
        "state_version_deleted",
        workspace=ws.name,
        serial=sv.serial,
        state_version_id=str(sv.id),
        deleted_by=user.email,
    )

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "state_version_created")

    return Response(status_code=204)


@router.post("/state-versions/{state_version_id}/actions/rollback")
async def rollback_state_version(
    request: Request,
    state_version_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Rollback to an older state version. Requires write permission.

    Creates a NEW state version with the content of the specified version
    and serial = max existing serial + 1. This is a "copy forward" rollback
    — no versions are deleted, history is preserved.
    """
    sv = await _get_state_version(state_version_id, db)
    ws = await _require_sv_workspace_capability(sv, cap.STATE_WRITE, user, db)

    # Download the old state bytes (decrypting if app-layer state encryption was
    # on when they were written, #635). Working in plaintext keeps md5/state_size
    # below consistent; the re-store re-encrypts under the active DEK.
    from terrapod.crypto.state import decrypt_state_bytes, encrypt_state_bytes

    storage = get_storage()
    old_key = state_key(str(sv.workspace_id), str(sv.id))
    try:
        state_bytes = await decrypt_state_bytes(await storage.get(old_key))
    except Exception:
        raise HTTPException(
            status_code=404,
            detail="State data not found in storage",
        ) from None

    # Determine next serial
    max_serial_result = await db.execute(
        select(func.max(StateVersion.serial)).where(StateVersion.workspace_id == sv.workspace_id)
    )
    max_serial = max_serial_result.scalar_one() or 0
    new_serial = max_serial + 1

    # The copy must carry the serial of the row it is stored under, not the
    # serial of the version it was copied from -- otherwise the next apply
    # computes a serial that already exists and its state upload is refused
    # after the infrastructure has changed (#1702). Rolling back to the current
    # version is also how a workspace already in that condition recovers.
    # Terraform/OpenTofu state only: a Pulumi deployment has no such serial,
    # and its row serial is Terrapod's own version counter.
    if ws.engine != "pulumi":
        try:
            state_bytes = await asyncio.to_thread(_state_with_serial, state_bytes, new_serial)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail="Stored state is not valid state JSON; it cannot be rolled back to",
            ) from exc

    # Hash off the event loop — state files are multi-MB and hashlib blocks
    md5_digest = await asyncio.to_thread(lambda: hashlib.md5(state_bytes).hexdigest())  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5
    # sha256 too: the runner's same-serial no-op check prefers it, and a row
    # without one falls back to md5 (#1702).
    sha256_digest = await asyncio.to_thread(lambda: hashlib.sha256(state_bytes).hexdigest())

    # Create new state version record
    new_sv = StateVersion(
        workspace_id=sv.workspace_id,
        serial=new_serial,
        lineage=sv.lineage,
        md5=md5_digest,
        sha256=sha256_digest,
        state_size=len(state_bytes),
        created_by=user.email,
    )
    db.add(new_sv)
    await db.flush()

    # A rollback advances the state serial → any apply-capable planned run now has
    # a stale plan; auto-discard them (#647).
    from terrapod.services import run_service

    await run_service.discard_stale_plans_for_state_change(db, sv.workspace_id, new_serial)

    # Store state bytes at new key (re-encrypted under the active DEK when on)
    new_key = state_key(str(sv.workspace_id), str(new_sv.id))
    await storage.put(new_key, await encrypt_state_bytes(state_bytes))

    await db.commit()
    await db.refresh(new_sv)

    # The break-glass index names every workspace's latest state (#1581).
    from terrapod.services import state_index_service

    await state_index_service.record_latest_state(
        workspace_name=ws.name,
        workspace_id=sv.workspace_id,
        state_version_id=new_sv.id,
        serial=new_serial,
    )

    logger.info(
        "state_version_rolled_back",
        workspace=ws.name,
        from_serial=sv.serial,
        to_serial=new_serial,
        state_version_id=str(new_sv.id),
        rolled_back_by=user.email,
    )

    from terrapod.api.metrics import STATE_VERSIONS_CREATED

    STATE_VERSIONS_CREATED.inc()

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "state_version_created")

    from terrapod.api.routers.tfe_v2 import _state_version_json

    return JSONResponse(
        content=_state_version_json(new_sv, request),
        status_code=201,
    )


@router.post("/workspaces/{workspace_id}/state-versions/actions/upload")
async def upload_state_manual(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Upload a state file manually. Requires write permission.

    Accepts raw state JSON. Serial is auto-assigned as max existing + 1
    to prevent conflicts. Lineage is extracted from the state file.
    """
    from terrapod.api.routers.tfe_v2 import _get_workspace_by_id

    ws = await _get_workspace_by_id(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.STATE_WRITE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires state:write capability on workspace",
        )

    # Stream the state body to a capped tempfile on the ephemeral PVC rather
    # than buffering it with `await request.body()` — a manually-uploaded
    # state can be multi-MB and would OOM the API pod (CLAUDE.md #14). The
    # lineage + md5 are read back off the event loop (#13).
    tmp_path, state_size = await stream_to_tempfile(request, suffix=".state.json")
    try:
        if state_size == 0:
            raise HTTPException(status_code=400, detail="Empty request body")

        # A Pulumi stack's state is its deployment, and what an operator has to
        # hand is `pulumi stack export` output, which wraps it. It is stored
        # unwrapped, as the service surface stores it, or every later read of
        # the stack would find the wrapper where the deployment should be (#1564).
        pulumi_payload: bytes | None = None
        sha256 = ""
        if ws.engine == "pulumi":
            try:
                pulumi_payload, md5, sha256 = await asyncio.to_thread(_read_pulumi_export, tmp_path)
            except (ValueError, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=400, detail=f"Invalid Pulumi state: {exc}") from exc
            lineage, state_size = "", len(pulumi_payload)
        else:
            try:
                lineage, md5 = await asyncio.to_thread(_read_state_lineage_md5, tmp_path)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=400, detail="Invalid state JSON") from exc

        # Auto-assign serial
        max_serial_result = await db.execute(
            select(func.max(StateVersion.serial)).where(StateVersion.workspace_id == ws.id)
        )
        max_serial = max_serial_result.scalar_one() or 0
        new_serial = max_serial + 1

        # Store the file under its row's serial, so the two agree (#1702). The
        # uploaded file's own serial is whatever the operator's copy said,
        # and left as-is it sends the next apply to a serial already taken.
        # Not for Pulumi: its payload is the unwrapped deployment, already
        # hashed above, and a deployment carries no Terraform serial.
        if pulumi_payload is None:
            try:
                md5, sha256, state_size = await asyncio.to_thread(
                    _rewrite_state_file_serial, tmp_path, new_serial
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid state JSON") from exc

        sv = StateVersion(
            workspace_id=ws.id,
            serial=new_serial,
            lineage=lineage,
            md5=md5,
            sha256=sha256,
            state_size=state_size,
            created_by=user.email,
        )
        db.add(sv)
        await db.flush()

        # Stream straight to storage when state encryption is off; when on,
        # envelope the whole blob first (#635). md5/state_size above are over the
        # plaintext, which is what downloads/divergence checks compare.
        from terrapod.crypto.state import encrypt_state_bytes, state_encryption_active

        storage = get_storage()
        key = state_key(str(ws.id), str(sv.id))
        if pulumi_payload is not None:
            await storage.put(
                key,
                await encrypt_state_bytes(pulumi_payload),
                content_type="application/octet-stream",
            )
        elif state_encryption_active():
            plaintext = await asyncio.to_thread(read_file_bytes, tmp_path)
            await storage.put(
                key, await encrypt_state_bytes(plaintext), content_type="application/octet-stream"
            )
        else:
            await storage.put_stream(
                key, file_chunks(tmp_path), content_type="application/octet-stream"
            )

        # A manual upload advances the state serial → any apply-capable planned
        # run now has a stale plan; auto-discard them (#647).
        from terrapod.services import run_service

        await run_service.discard_stale_plans_for_state_change(db, ws.id, new_serial)

        await db.commit()
        await db.refresh(sv)
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    # The break-glass index names every workspace's latest state (#1581).
    from terrapod.services import state_index_service

    await state_index_service.record_latest_state(
        workspace_name=ws.name, workspace_id=ws.id, state_version_id=sv.id, serial=new_serial
    )

    logger.info(
        "state_version_uploaded_manually",
        workspace=ws.name,
        serial=new_serial,
        state_version_id=str(sv.id),
        uploaded_by=user.email,
    )

    from terrapod.api.metrics import STATE_VERSIONS_CREATED

    STATE_VERSIONS_CREATED.inc()

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "state_version_created")

    from terrapod.api.routers.tfe_v2 import _state_version_json

    return JSONResponse(
        content=_state_version_json(sv, request),
        status_code=201,
    )
