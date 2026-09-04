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
from terrapod.api.upload_stream import file_chunks, read_file_bytes, stream_to_tempfile
from terrapod.config import settings
from terrapod.db.models import Run, StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.plan_summary import summarize_plan_json
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    apply_log_key,
    config_version_key,
    cost_estimate_key,
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


def _read_state_metadata(path: str) -> tuple[int, str, str, str]:
    """Read (serial, lineage, md5, sha256) from a state file on disk.

    Runs in a worker thread (CLAUDE.md #13): the hashes are computed over
    the full state and `json.load` parses it, both of which would block the
    event loop on a multi-MB state if run inline. The file lives on the
    ephemeral PVC, never in the worker heap.

    md5 is kept for the TFE/go-tfe state-version contract; sha256 is the
    hash used for the divergence equality check (an md5 collision must not
    suppress a genuine divergence flag). Both are computed in one pass.
    """
    h_md5 = hashlib.md5()  # noqa: S324  # nosemgrep: insecure-hash-algorithm-md5
    h_sha = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(1024 * 1024)
            if not buf:
                break
            h_md5.update(buf)
            h_sha.update(buf)
    with open(path, "rb") as fh:
        state_data = json.load(fh)
    serial = state_data.get("serial", 0)
    lineage = state_data.get("lineage", "")
    return serial, lineage, h_md5.hexdigest(), h_sha.hexdigest()


def _summarize_plan_file(path: str) -> dict | None:
    """Read a plan-JSON tempfile and summarise it (worker thread).

    `summarize_plan_json` parses the JSON, which would block the event loop
    on a multi-MB plan; the file lives on the ephemeral PVC, not the heap.
    """
    with open(path, "rb") as fh:
        return summarize_plan_json(fh.read())


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
) -> Response:
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

    # When app-layer state encryption is on (#635) the object is ciphertext, so a
    # presigned redirect would hand the runner an unreadable blob — proxy it
    # through the API and decrypt instead. When off (the default) keep the
    # zero-copy presigned redirect: no behaviour change for the common case.
    from terrapod.crypto.state import decrypt_state_bytes, state_encryption_active

    if state_encryption_active():
        data = await decrypt_state_bytes(await storage.get(key))
        return Response(content=data, media_type="application/octet-stream")

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

    storage = get_storage()
    key = plan_log_key(str(run.workspace_id), str(run.id))
    # Stream straight to storage — plan logs can be large; never buffer the
    # whole body in the API's RAM (rule 14).
    await storage.put_stream(key, request.stream())
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

    storage = get_storage()
    key = plan_output_key(str(run.workspace_id), str(run.id))
    # Stream the (potentially large) binary plan straight to storage (rule 14).
    await storage.put_stream(key, request.stream())
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

    storage = get_storage()
    key = lock_file_key(str(run.workspace_id), str(run.id))
    await storage.put_stream(key, request.stream())
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

    # Stream the plan JSON to a capped tempfile on the ephemeral PVC instead
    # of buffering it with `await request.body()` — plan JSON can be many MB
    # and would OOM the API pod on a large plan (CLAUDE.md #14). The summary
    # parse reads the tempfile back in a worker thread (#13).
    tmp_path, body_bytes = await stream_to_tempfile(request, suffix=".plan.json")
    try:
        storage = get_storage()
        key = plan_json_output_key(str(run.workspace_id), str(run.id))
        # Order matters: write storage first, then flip the flag. If the
        # commit fails after a successful upload, the artifact is reachable
        # only via retention sweep — annoying, but better than the reverse,
        # which would advertise a URL pointing at nothing.
        await storage.put_stream(key, file_chunks(tmp_path), content_type="application/json")
        run.has_json_output = True
        # Parse the plan in a thread (reading the tempfile, never the raw
        # request body) so a multi-MB JSON doesn't block the event loop. A
        # parse failure leaves the count columns null — the download URL is
        # still served, just no UI summary.
        summary = await asyncio.to_thread(_summarize_plan_file, tmp_path)
        if summary is not None:
            run.resource_additions = summary["additions"]
            run.resource_changes = summary["changes"]
            run.resource_destructions = summary["destructions"]
            run.resource_replacements = summary["replacements"]
            run.resource_imports = summary["imports"]
            conditional_auto_apply = True
        else:
            # Counts unknown, so the plan's shape is unknown: a conditional
            # auto-apply must not run. Leaving it `planned` is the point.
            conditional_auto_apply = False
            logger.warning(
                "plan_json_output.summary_unparseable",
                run_id=str(run.id),
                workspace_id=str(run.workspace_id),
                body_bytes=body_bytes,
            )
        await db.commit()
        # Conditional auto-apply (#1274) decides HERE, not in `complete_plan`:
        # this is the first point at which the plan's shape is known, because
        # the runner POSTs plan-result (which drives complete_plan) before it
        # uploads this JSON. A run whose counts didn't parse is left alone —
        # the helper treats unknown shape as "do not auto-apply".
        if conditional_auto_apply:
            from terrapod.services import run_service

            await run_service.evaluate_conditional_auto_apply(db, run)
        return await _enqueue_plan_json_followups(run, db)
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass


async def _enqueue_plan_json_followups(run: Run, db: AsyncSession) -> Response:
    """Fire the AI-summary + drift-completion triggers after plan-JSON lands.

    Split out of `upload_plan_json_output` so the streamed-tempfile
    try/finally stays compact. Both triggers re-fire here (rather than from
    `run_service.transition_run`) to close the race where the runner POSTs
    plan-result before uploading plan-json-output.
    """
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
            from terrapod.services.summariser import mark_summary_queued

            await enqueue_trigger(
                "ai_plan_summary",
                {"run_id": str(run.id), "kind": "plan_summary"},
                dedup_key=f"aisum:{run.id}:plan_summary",
                dedup_ttl=300,
            )
            # Placeholder row so the summary endpoint stops 404ing the moment
            # the work exists, rather than only once a consumer picks it up
            # (#1295). Written after the enqueue so a failed enqueue leaves no
            # row: a summary stuck at `pending` forever would be a worse lie
            # than the 404 it replaces. Insert-if-absent, so if a consumer has
            # already finished — entirely possible now the AI lane runs several
            # at once — the real result stands.
            await mark_summary_queued(db, run_id=run.id, kind="plan_summary")
            await db.commit()
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


def _summarize_cost_file(path: str) -> tuple[str | None, float | None, float | None] | None:
    """Read (currency, monthly_min, monthly_max) from a cost_estimate.json.

    Reads + parses on disk (never the raw request body) so it can run in a
    worker thread off the event loop. Returns None on any parse/shape error —
    the artifact is still stored and served; only the cached totals are skipped.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
        total = data.get("total") or {}
        currency = data.get("currency")
        return (
            currency if isinstance(currency, str) else None,
            float(total["min"]) if total.get("min") is not None else None,
            float(total["max"]) if total.get("max") is not None else None,
        )
    except OSError, ValueError, KeyError, TypeError:
        return None


@router.put("/runs/{run_id}/artifacts/cost-estimate")
async def upload_cost_estimate(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the run's cost estimate (`cost_estimate.json`, #871).

    Produced by the runner from the plan JSON via the native cost engine. Sets
    `runs.has_cost_estimate = true` (gating the Cost tab + download URL) and
    caches the plan-total monthly range for cheap list display. Advisory: a
    parse failure still stores the artifact, just without the cached totals.
    """
    require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    # Small artifact (bounded by resource count) but streamed to a tempfile for
    # consistency with the other artifact uploads; parsed in a thread (#13).
    tmp_path, _body_bytes = await stream_to_tempfile(request, suffix=".cost.json")
    try:
        storage = get_storage()
        key = cost_estimate_key(str(run.workspace_id), str(run.id))
        # Storage first, then the flag (same order as plan-json): a commit
        # failure after upload leaves an orphan artifact, never an advertised
        # URL pointing at nothing.
        await storage.put_stream(key, file_chunks(tmp_path), content_type="application/json")
        run.has_cost_estimate = True
        totals = await asyncio.to_thread(_summarize_cost_file, tmp_path)
        if totals is not None:
            run.cost_currency, run.cost_monthly_min, run.cost_monthly_max = totals
        else:
            logger.warning(
                "cost_estimate.summary_unparseable",
                run_id=str(run.id),
                workspace_id=str(run.workspace_id),
            )
        await db.commit()
        # AI cost narrative (#871) — enqueue the enhancement now that the
        # estimate is in storage and the flag is committed. Rides the same
        # switch as the plan summary; best-effort, deduped per run. The handler
        # re-checks the flag + workspace mode, so a disabled workspace no-ops.
        if settings.ai_summary.enabled:
            try:
                from terrapod.services.scheduler import enqueue_trigger

                await enqueue_trigger(
                    "ai_cost_summary",
                    {"run_id": str(run.id)},
                    dedup_key=f"aicost:{run.id}",
                    dedup_ttl=300,
                )
            except Exception as e:
                logger.debug("Failed to enqueue ai_cost_summary after upload", error=str(e))
        return Response(status_code=204)
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass


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

    storage = get_storage()
    key = apply_log_key(str(run.workspace_id), str(run.id))
    # Stream straight to storage — apply logs can be large (rule 14).
    await storage.put_stream(key, request.stream())
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

    # Stream the state body to a capped tempfile on the ephemeral PVC rather
    # than buffering it in the worker heap — runner state uploads can be
    # multi-MB and `await request.body()` would accumulate the whole thing in
    # RAM, OOM-killing the API pod on a large state (CLAUDE.md #14). Metadata
    # (serial/lineage/md5) is then read back off the event loop (#13).
    tmp_path, state_size = await stream_to_tempfile(request, suffix=".state.json")
    try:
        try:
            serial, lineage, md5, sha256 = await asyncio.to_thread(_read_state_metadata, tmp_path)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid state JSON") from exc

        return await _persist_runner_state(
            db, run, run_id, tmp_path, state_size, serial, lineage, md5, sha256
        )
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass


async def _persist_runner_state(
    db: AsyncSession,
    run: Run,
    run_id: str,
    tmp_path: str,
    state_size: int,
    serial: int,
    lineage: str,
    md5: str,
    sha256: str,
) -> Response:
    """Divergence check + StateVersion insert + stream-to-storage for a state upload.

    Split out of `upload_state` so the streamed-tempfile lifecycle (the
    caller's try/finally) stays small and the parsing/divergence logic reads
    linearly. The tempfile at `tmp_path` is owned by the caller.
    """
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
        # Prefer the collision-resistant sha256 for the equality check; an md5
        # collision must not be able to make two distinct states compare equal
        # and suppress a genuine divergence flag. Fall back to md5 only when the
        # existing row predates the sha256 column (legacy rows have sha256 == "").
        if existing.sha256:
            states_match = existing.sha256 == sha256
        else:
            states_match = existing.md5 == md5
        if states_match:
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
        sha256=sha256,
        state_size=state_size,
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

    # Store at canonical key (same format used by download_state). With state
    # encryption off (default) stream the tempfile straight into storage —
    # constant memory, never re-buffered. With it on (#635) the blob must be
    # enveloped first; read + encrypt off the event loop, then store the
    # ciphertext (md5/sha256/state_size above are over the plaintext, which the
    # divergence checks compare).
    from terrapod.crypto.state import encrypt_state_bytes, state_encryption_active

    storage = get_storage()
    key = state_key(str(run.workspace_id), str(sv.id))
    if state_encryption_active():
        plaintext = await asyncio.to_thread(read_file_bytes, tmp_path)
        await storage.put(
            key, await encrypt_state_bytes(plaintext), content_type="application/octet-stream"
        )
    else:
        await storage.put_stream(
            key, file_chunks(tmp_path), content_type="application/octet-stream"
        )

    # Clear state_diverged flag on successful state upload
    ws = await db.get(Workspace, run.workspace_id)
    if ws and ws.state_diverged:
        ws.state_diverged = False

    # This apply advanced the workspace state → any OTHER apply-capable planned
    # run now has a stale plan; auto-discard them (#647). The applying run itself
    # is not `planned`, but exclude it explicitly for clarity.
    from terrapod.services import run_service

    await run_service.discard_stale_plans_for_state_change(
        db, run.workspace_id, serial, exclude_run_id=run.id
    )

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


# ── Onboarding discovery artifacts (#824 P2 — D2/D3) ───────────────────
# The discovery Job uploads its generated artifacts here; each resolves the
# owning OnboardingSession via its ``discovery_run_id`` (== this run) and writes
# the column directly. The runner token is scoped to the discovery run id, so a
# Job can only ever write to its own session. Config/imports are .tf TEXT that
# lands in a DB row (small-file class — a handful of resources), capped to guard
# the pod (a very large estate that outgrows a row is a documented follow-up).

_ONBOARDING_MAX_BYTES = 10 * 1024 * 1024


async def _get_onboarding_session_for_run(run_id: str, db: AsyncSession):
    from terrapod.db.models import OnboardingSession

    result = await db.execute(
        select(OnboardingSession).where(OnboardingSession.discovery_run_id == uuid.UUID(run_id))
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="No onboarding session for this run")
    return session


async def _read_capped_text(request: Request) -> str:
    body = await request.body()
    if len(body) > _ONBOARDING_MAX_BYTES:
        raise HTTPException(status_code=413, detail="onboarding artifact too large")
    return body.decode("utf-8", errors="replace")


@router.put("/runs/{run_id}/artifacts/onboarding-config")
async def upload_onboarding_config(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The cleaned, import-only generated `resource {}` config (D3 + clean)."""
    require_runner_for_run(user, run_id)
    session = await _get_onboarding_session_for_run(run_id, db)
    session.generated_config = await _read_capped_text(request)
    await db.commit()
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/onboarding-imports")
async def upload_onboarding_imports(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The candidate `import {}` blocks (D3)."""
    require_runner_for_run(user, run_id)
    session = await _get_onboarding_session_for_run(run_id, db)
    session.import_blocks = await _read_capped_text(request)
    await db.commit()
    return Response(status_code=204)


@router.post("/runs/{run_id}/artifacts/onboarding-query-results")
async def post_onboarding_query_results(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The raw D2 query results + the import-only verdict (JSON)."""
    require_runner_for_run(user, run_id)
    session = await _get_onboarding_session_for_run(run_id, db)
    try:
        payload = await request.json()
    except ValueError, UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="body must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    session.query_results = payload
    await db.commit()
    return Response(status_code=204)
