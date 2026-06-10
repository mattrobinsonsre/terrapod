"""Authenticated artifact download/upload endpoints for runner Jobs.

Runners authenticate with a short-lived runner token (HMAC-signed, scoped
to a single run). The token's run_id must match the path run_id.

Downloads return 302 redirects to presigned storage URLs.
Uploads accept raw bytes and write to storage directly.

Endpoints:
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/config         — download config archive
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/state           — download current state
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/plan-file       — download plan file
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/lock-file       — download .terraform.lock.hcl from plan
    GET  /api/terrapod/v1/runs/{run_id}/artifacts/plan-artifacts  — download plan-phase workspace diff tarball
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/plan-log        — upload plan log
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/plan-file       — upload plan file
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/lock-file       — upload .terraform.lock.hcl from plan
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/plan-artifacts  — upload plan-phase workspace diff tarball (streamed)
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/plan-json-output — upload plan JSON
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/apply-log       — upload apply log
    PUT  /api/terrapod/v1/runs/{run_id}/artifacts/state           — upload new state
"""

import asyncio
import hashlib
import json
import os
import tempfile
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_runner_for_run
from terrapod.config import settings
from terrapod.db.models import Run, StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.plan_summary import summarize_plan_json
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    apply_log_key,
    config_version_key,
    lock_file_key,
    plan_artifacts_key,
    plan_json_output_key,
    plan_log_key,
    plan_output_key,
    state_key,
)

router = APIRouter(tags=["run-artifacts"])
logger = get_logger(__name__)


async def _get_run(run_id: str, db: AsyncSession) -> Run:
    """Get a run by UUID string."""
    run = await db.get(Run, uuid.UUID(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def _publish_log_updated(workspace_id: str, run_id: str, phase: str) -> None:
    """Notify the UI that a fresh log artifact landed in storage.

    The runner's EXIT trap uploads the authoritative final log to storage
    AFTER it POSTs plan-result / apply-result and the run has already
    transitioned to a terminal state. Without this notification the UI
    sits on the last Redis-snapshot it fetched mid-flight: `_serve_log`
    correctly omits ETX on the Redis path so polling stays open, but the
    UI only triggers a re-fetch when a `log_updated` event arrives. The
    mid-flight listener `upload_log_stream` emits one per chunk; without
    a corresponding emit here, the trailing bytes from the EXIT trap are
    invisible until the user hits Refresh.
    """
    try:
        from terrapod.redis.client import RUN_EVENTS_PREFIX, publish_event

        payload = json.dumps(
            {
                "event": "log_updated",
                "run_id": run_id,
                "workspace_id": workspace_id,
                "phase": phase,
            }
        )
        await publish_event(f"{RUN_EVENTS_PREFIX}{workspace_id}", payload)
    except Exception:
        # Match upload_log_stream: SSE publishing failures must never break
        # an in-flight artifact upload. Worst case we fall back to the old
        # behaviour (UI waits until next event or manual refresh).
        logger.debug("Failed to publish log_updated after artifact upload")


# ── Downloads (302 redirect to presigned GET URL) ────────────────────────


@router.get("/runs/{run_id}/artifacts/config")
async def download_config(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the configuration archive for a run."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    if not run.configuration_version_id:
        raise HTTPException(status_code=404, detail="No configuration version")

    storage = get_storage()
    key = config_version_key(str(run.workspace_id), str(run.configuration_version_id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/state")
async def download_state(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the current state for the run's workspace."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    result = await db.execute(
        select(StateVersion)
        .where(StateVersion.workspace_id == run.workspace_id)
        .order_by(StateVersion.serial.desc())
        .limit(1)
    )
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="No state version")

    storage = get_storage()
    key = state_key(str(run.workspace_id), str(sv.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/plan-file")
async def download_plan_file(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the plan file from the plan phase."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    storage = get_storage()
    key = plan_output_key(str(run.workspace_id), str(run.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/lock-file")
async def download_lock_file(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the `.terraform.lock.hcl` produced by the plan-phase init.

    Carried into the apply phase so apply's `terraform init` resolves to
    the same provider versions plan used, rather than re-evaluating the
    version constraint and potentially picking up a newer matching
    version published in the plan→apply window. See #306.

    The runner treats a 404/non-2xx here as a warning, not an error — the
    apply phase still works (with the today-behaviour drift risk) when
    the plan ran on an older runner that didn't upload a lock file.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    storage = get_storage()
    key = lock_file_key(str(run.workspace_id), str(run.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


# ── Uploads (receive body, write to storage) ─────────────────────────────


@router.put("/runs/{run_id}/artifacts/plan-log")
async def upload_plan_log(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the plan log."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_log_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    await _publish_log_updated(str(run.workspace_id), str(run.id), "plan")
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/plan-file")
async def upload_plan_file(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the plan file."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_output_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/lock-file")
async def upload_lock_file(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the `.terraform.lock.hcl` produced by the plan-phase init.

    See `download_lock_file` for the rationale. The runner treats this
    upload as best-effort — a failure here just means the apply phase
    falls back to re-resolving providers (today's behaviour).
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = lock_file_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/plan-json-output")
async def upload_plan_json_output(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the structured JSON plan output (`tofu show -json tfplan`).

    Sets `runs.has_json_output = true` so plan responses can advertise
    the read URL with confidence (errored / older / failed-upload runs
    leave the flag at its default `false`).
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_json_output_key(str(run.workspace_id), str(run.id))
    # Order matters: write storage first, then flip the flag. If the
    # commit fails after a successful upload, the artifact is reachable
    # only via retention sweep — annoying, but better than the reverse,
    # which would advertise a URL pointing at nothing.
    await storage.put(key, body)
    run.has_json_output = True
    # Parse the plan in a thread so a multi-MB JSON doesn't block the
    # event loop. A parse failure leaves the count columns null — the
    # download URL is still served, just no UI summary.
    summary = await asyncio.to_thread(summarize_plan_json, body)
    if summary is not None:
        run.resource_additions = summary["additions"]
        run.resource_changes = summary["changes"]
        run.resource_destructions = summary["destructions"]
        run.resource_replacements = summary["replacements"]
        run.resource_imports = summary["imports"]
    else:
        logger.warning(
            "plan_json_output.summary_unparseable",
            run_id=str(run.id),
            workspace_id=str(run.workspace_id),
            body_bytes=len(body),
        )
    await db.commit()

    # AI plan summariser (#401) — enqueue the `plan_summary` kind now
    # that the JSON is actually in storage. Previously this fired from
    # run_service.transition_run on the planned transition, which
    # raced the runner: transition_run runs on the plan-result POST,
    # which the runner sends BEFORE uploading plan-json-output. The
    # summariser would then hit "Object not found" half the time and
    # write status='errored'. Firing here closes the race — by the
    # time the trigger is enqueued the storage put + db commit have
    # both succeeded. Failure-analysis kind still fires from
    # transition_run on errored runs (no JSON involved).
    if settings.ai_summary.enabled:
        try:
            from terrapod.services.scheduler import enqueue_trigger

            await enqueue_trigger(
                "ai_plan_summary",
                {"run_id": str(run.id), "kind": "plan_summary"},
                dedup_key=f"aisum:{run.id}:plan_summary",
                dedup_ttl=300,
            )
        except Exception as e:
            logger.debug("Failed to enqueue ai_plan_summary after upload", error=str(e))

    # Drift-ignore classifier (#482) — same race as the AI summariser.
    # `handle_drift_run_completed` fires from run_service.transition_run
    # on the `planned` transition, which the runner POSTs BEFORE
    # uploading plan-json-output. So when a workspace has
    # `drift_ignore_rules` configured, that first pass finds
    # `has_json_output == False`, can't fetch the plan to classify, and
    # conservatively leaves drift_status = "drifted". Re-enqueue the
    # completion handler now that the JSON is committed; it re-runs with
    # `has_json_output == True` and the classifier flips drift_status to
    # "no_drift" when every change matches a rule. Distinct dedup key so
    # this re-trigger isn't swallowed by the transition-time enqueue's
    # `drift:{run_id}` dedup window. Only drift runs need this; normal
    # runs don't touch drift_status.
    if run.is_drift_detection:
        try:
            from terrapod.services.scheduler import enqueue_trigger

            await enqueue_trigger(
                "drift_run_completed",
                {"run_id": str(run.id), "workspace_id": str(run.workspace_id)},
                dedup_key=f"drift_postjson:{run.id}",
                dedup_ttl=300,
            )
        except Exception as e:
            logger.debug("Failed to re-enqueue drift completion after upload", error=str(e))

    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/apply-log")
async def upload_apply_log(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the apply log."""
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = apply_log_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    await _publish_log_updated(str(run.workspace_id), str(run.id), "apply")
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/state")
async def upload_state(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload new state after apply.

    Parses the uploaded state JSON, creates a StateVersion record, and
    stores the state at the canonical key so that subsequent plans can
    find it via the standard state download path.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()

    # Parse state JSON to extract metadata
    try:
        state_data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid state JSON") from exc

    serial = state_data.get("serial", 0)
    lineage = state_data.get("lineage", "")
    # Hash off the event loop — runner state uploads can be multi-MB
    md5 = await asyncio.to_thread(lambda: hashlib.md5(body).hexdigest())  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5

    # tofu/terraform does NOT bump the state serial when an apply leaves the
    # persisted state byte-identical to the prior state. This happens whenever a
    # resource carries a *perpetual phantom diff* — write-only attributes that are
    # re-sent on every apply (e.g. auth0 client secrets), values the provider
    # normalises, etc. The plan reports "1 changed", the apply calls the provider's
    # Update, but the resulting state equals the prior state, so the serial is
    # unchanged. That is NOT a divergence: the API's state already matches the
    # state the runner holds. Treat an identical (same serial + same md5) upload as
    # an idempotent no-op success rather than flagging state-diverged.
    #
    # Only a *different* state body at an already-recorded serial is a genuine
    # conflict (two distinct states claiming the same serial → real divergence) →
    # 409. The IntegrityError catch on the INSERT below closes the race window
    # where a concurrent upload inserts between our SELECT and INSERT.
    _existing_serial_msg = (
        f"State serial {serial} already exists for this workspace with different "
        "content. The runner's post-apply state diverged from the recorded state "
        "at this serial."
    )
    existing = (
        await db.execute(
            select(StateVersion).where(
                StateVersion.workspace_id == run.workspace_id,
                StateVersion.serial == serial,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.md5 == md5:
            # Serial-neutral no-op apply: state is provably identical. Clear any
            # stale divergence flag and return success so the runner does NOT
            # signal state-diverged and the run transitions to applied.
            ws = await db.get(Workspace, run.workspace_id)
            if ws and ws.state_diverged:
                ws.state_diverged = False
                await db.commit()
            logger.info(
                "state_upload_noop_serial_unchanged",
                run_id=run_id,
                workspace_id=str(run.workspace_id),
                serial=serial,
            )
            return Response(status_code=200)
        raise HTTPException(status_code=409, detail=_existing_serial_msg)

    # Create StateVersion record
    sv = StateVersion(
        workspace_id=run.workspace_id,
        serial=serial,
        lineage=lineage,
        md5=md5,
        state_size=len(body),
        run_id=run.id,
        created_by=run.created_by or None,
    )
    db.add(sv)
    try:
        await db.flush()
    except IntegrityError:
        # Race: another upload inserted the same (workspace_id, serial)
        # between our SELECT and INSERT. Roll back so the session is
        # usable for any caller-side cleanup, then return 409.
        await db.rollback()
        raise HTTPException(status_code=409, detail=_existing_serial_msg) from None

    # Store at canonical key (same format used by download_state)
    storage = get_storage()
    key = state_key(str(run.workspace_id), str(sv.id))
    await storage.put(key, body)

    # Clear state_diverged flag on successful state upload
    ws = await db.get(Workspace, run.workspace_id)
    if ws and ws.state_diverged:
        ws.state_diverged = False

    await db.commit()
    logger.info(
        "state_version_created_from_runner",
        run_id=run_id,
        workspace_id=str(run.workspace_id),
        state_version_id=str(sv.id),
        serial=serial,
    )

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(run.workspace_id), "state_version_created")

    return Response(status_code=204)


@router.post("/runs/{run_id}/resource-profile")
async def record_resource_profile(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Record the runner Job's resource-usage peak (#430).

    Called by the runner entrypoint at exit (via the EXIT trap, so it
    fires on every normal exit path — clean success, plan errored,
    OPA failed, SIGTERM during apply, etc.).

    Captures:
        peak_memory_bytes — /sys/fs/cgroup/memory.peak (cgroup v2)
        peak_cpu_usec     — cumulative usage_usec from /sys/fs/cgroup/cpu.stat
        exit_code         — the runner script's actual exit code (0 = clean)

    Body shape (JSON):
        { "peak_memory_bytes": <int>, "peak_cpu_usec": <int>, "exit_code": <int> }

    For OOMKill / external SIGKILL the runner's trap doesn't fire — those
    cases are filled in by the listener's job-status report (run_reconciler
    reads the K8s container terminated state and writes runner_exit_reason
    + runner_exit_status separately). Both paths converge on the same DB
    columns; whichever signal arrives wins. The runner_exit_status field
    is *only* set by the reconciler (never by the runner directly) so the
    typed bucketing stays in one place.

    Runner-token auth, scoped to this run_id.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    # All three fields optional — the runner sends what it could read.
    # Negative / non-int values are rejected to keep the DB schema sane.
    def _opt_nonneg_int(name: str) -> int | None:
        v = body.get(name)
        if v is None:
            return None
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise HTTPException(
                status_code=400,
                detail=f"{name} must be a non-negative integer, got {v!r}",
            )
        return v

    peak_memory_bytes = _opt_nonneg_int("peak_memory_bytes")
    peak_cpu_usec = _opt_nonneg_int("peak_cpu_usec")
    exit_code = _opt_nonneg_int("exit_code")

    if peak_memory_bytes is not None:
        run.peak_memory_bytes = peak_memory_bytes
    if peak_cpu_usec is not None:
        run.peak_cpu_usec = peak_cpu_usec
    if exit_code is not None:
        run.runner_exit_code = exit_code

    await db.commit()

    logger.info(
        "runner_resource_profile_recorded",
        run_id=run_id,
        peak_memory_bytes=peak_memory_bytes,
        peak_cpu_usec=peak_cpu_usec,
        exit_code=exit_code,
    )

    return Response(status_code=204)


@router.post("/runs/{run_id}/state-diverged")
async def mark_state_diverged(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Mark a workspace as having diverged state.

    Called by the runner entrypoint when a state upload fails after a
    successful apply. The workspace is flagged so the UI can warn users.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    ws.state_diverged = True
    await db.commit()

    logger.warning(
        "workspace_state_diverged",
        run_id=run_id,
        workspace_id=str(run.workspace_id),
    )

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(run.workspace_id), "state_diverged")

    return Response(status_code=204)


# ── Plan-artifacts tarball (workspace diff between init and plan) ────────


def _resolve_ephemeral_tmpdir() -> str | None:
    """Resolve the API pod's ephemeral-storage PVC mount.

    Matches the pattern used by `cv_diff_service._resolve_tmpdir`,
    `vcs_archive_cache._resolve_tmpdir`,
    `provider_cache_service._resolve_ephemeral_tmpdir`. On the API pod
    `/tmp` is a RAM-backed `emptyDir{}`; tempfiles that can plausibly
    grow to tens of MB MUST land on the dedicated PVC at
    `settings.vcs.tmpdir` (default `/var/lib/terrapod/tmp`). Returning
    `None` falls back to the system default for local dev and tests.
    """
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


@router.get("/runs/{run_id}/artifacts/plan-artifacts")
async def download_plan_artifacts(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the plan-phase workspace-diff tarball.

    Returned via 302 → presigned storage URL, matching the other
    artifact-download endpoints. The runner treats a 404 here as
    expected (older plans, plans that produced no new files); the
    apply phase proceeds without the restore.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    storage = get_storage()
    key = plan_artifacts_key(str(run.workspace_id), str(run.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.put("/runs/{run_id}/artifacts/plan-artifacts")
async def upload_plan_artifacts(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the plan-phase workspace-diff tarball.

    Streams the request body to a tempfile on the API pod's ephemeral
    PVC (`settings.vcs.tmpdir`) and then `put_stream`s the tempfile to
    object storage. Does NOT load the body into memory — required for
    the user-configurable 256 MiB default cap to be safe on small API
    pods.

    Cheap pre-check: if `Content-Length` exceeds the cap, refuse with
    HTTP 413 before opening the tempfile. Then enforce the cap again
    during streaming (HTTP clients may lie about Content-Length or omit
    it under chunked transfer encoding). The runner treats 413 as a
    skip-the-restore signal — apply proceeds without it.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    max_bytes = settings.runner_artifacts.plan_artifacts_max_bytes

    # Pre-check Content-Length when the client provides it (let the
    # runner give up faster than waiting on the full upload).
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(f"plan-artifacts upload too large: {declared} bytes > {max_bytes} cap"),
                )
        except ValueError:
            pass  # Malformed Content-Length — fall through to streamed enforcement.

    tmpdir = _resolve_ephemeral_tmpdir()
    fd, tmp_path = await asyncio.to_thread(
        tempfile.mkstemp, suffix=".plan-artifacts.tar", dir=tmpdir
    )
    f = await asyncio.to_thread(os.fdopen, fd, "wb")
    received = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"plan-artifacts upload exceeded the {max_bytes}-byte cap "
                        f"after streaming {received} bytes"
                    ),
                )
            await asyncio.to_thread(f.write, chunk)
        await asyncio.to_thread(f.flush)
        await asyncio.to_thread(f.close)

        # Stream the tempfile into storage (constant-memory put).
        async def _chunks():
            with open(tmp_path, "rb") as src:  # noqa: ASYNC230 -- bounded reads
                while True:
                    buf = await asyncio.to_thread(src.read, 1024 * 1024)
                    if not buf:
                        break
                    yield buf

        storage = get_storage()
        key = plan_artifacts_key(str(run.workspace_id), str(run.id))
        await storage.put_stream(key, _chunks(), content_type="application/x-tar")
    finally:
        if not f.closed:
            try:
                await asyncio.to_thread(f.close)
            except OSError:
                pass
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    return Response(status_code=204)
