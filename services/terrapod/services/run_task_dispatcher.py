"""Triggered task handler for run task webhook delivery.

Registered with the distributed scheduler as a trigger handler.
Receives {task_stage_result_id} payloads, loads context, and POSTs
to the external webhook URL with HMAC signature and callback info.
"""

import hashlib
import hmac
import json
import uuid
from datetime import UTC

from terrapod.db.models import (
    ConfigurationVersion,
    Run,
    RunTask,
    TaskStage,
    TaskStageResult,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import run_links
from terrapod.services.outbound_url_guard import validate_outbound_url
from terrapod.services.run_task_service import resolve_stage

logger = get_logger(__name__)

#: The signature header HashiCorp's run task integration spec defines. Sent
#: alongside Terrapod's own `X-TFE-Task-Signature`, never instead of it.
SPEC_SIGNATURE_HEADER = "X-TFC-Task-Signature"


def _rfc3339(dt) -> str:  # type: ignore[no-untyped-def]
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_task_callback_payload(
    run_id: str,
    run_created_at: str,
    run_message: str,
    run_created_by: str,
    run_app_url: str,
    workspace_id: str,
    workspace_name: str,
    workspace_app_url: str,
    workspace_working_directory: str,
    task_name: str,
    task_id: str,
    task_result_id: str,
    task_result_enforcement_level: str,
    stage: str,
    callback_url: str,
    access_token: str,
    is_speculative: bool,
    configuration_version_id: str | None,
    vcs_repo_url: str | None,
    vcs_branch: str | None,
    vcs_commit_hash: str | None,
) -> dict:
    """Build a TFE V2-compatible run task callback payload.

    **Every field here describes the run, so every field is an argument**
    (#2367). Six of them used to be hardcoded `""`/`False` while the values
    sat on rows the caller had already loaded, so an external service
    received a payload that was well-formed, stably shaped, and wrong about
    the run it described -- `is_speculative` worst of all, since a run
    against a speculative configuration version is exactly the one a
    PR-gating task wants to treat differently.

    **Two constants stay constant, and are not oversights.**
    `payload_version` is 1 because 1 is the only version defined, and
    `organization_name` is `"default"` because Terrapod is single-org by
    design (architecture principle 9) -- there is no other value to report.

    `run_status` used to be a parameter, resolved by the caller and never
    emitted. It is gone rather than added to the payload: a task's view of
    the run is the `stage` it was called at, and a new key is a payload
    addition nobody asked for. **Re-checked against HashiCorp's run task
    integration spec for #2366: there is no `run_status` field in it, so
    removing it was right and the 2.0 run-status fold (#2378) does not reach
    an integration through this payload at all.**

    ### The spec-shaped fields (#2366)

    Everything below `is_speculative` arrived for spec compatibility, and
    every one is a value Terrapod already held and simply never wired in --
    which is why they are additive rather than a decision.

    **Nullable on purpose, and nullable in the spec too.** A run with no
    configuration version, or on a workspace with no VCS connection, has no
    honest value for these; the spec types them "or `null`", so `None` is the
    documented absence rather than an omission. An omitted KEY is what breaks
    a strict client, which is the same reasoning as the `or ""` at the call
    site for the declared-string fields.

    **`vcs_commit_url` and `vcs_pull_request_url` are hardcoded `None`, and
    that is a gap rather than a value.** We hold the commit sha and the pull
    request NUMBER, not a web URL, and building one needs provider-aware
    construction -- GitHub `/pull/{n}` against GitLab `/-/merge_requests/{n}`,
    over a `server_url` that may carry nested groups -- which does not exist
    in this codebase yet. Emitting the keys as `null` is spec-legal and tells
    an integration the fact is absent; inventing a URL shape per provider
    inside a payload builder would be the wrong place for it. If they turn out
    to be wanted they are a small follow-up.

    **`capabilities` is what WE support, not what the caller does.** It reads
    backwards at first: the spec has the sender declare whether it accepts
    detailed outcomes in the callback. We do not accept them, so it is
    `false` -- stated rather than omitted, so an integration's graceful
    degradation is deterministic instead of inferred from a missing object.
    """
    return {
        "payload_version": 1,
        "stage": stage,
        "access_token": access_token,
        "capabilities": {"outcomes": False},
        "task_result_callback_url": callback_url,
        "task_result_id": task_result_id,
        "task_result_enforcement_level": task_result_enforcement_level,
        "run_app_url": run_app_url,
        "run_id": run_id,
        "run_message": run_message,
        "run_created_at": run_created_at,
        "run_created_by": run_created_by,
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "workspace_app_url": workspace_app_url,
        "workspace_working_directory": workspace_working_directory,
        "organization_name": "default",
        "task_name": task_name,
        "task_id": task_id,
        "is_speculative": is_speculative,
        "configuration_version_id": configuration_version_id,
        "vcs_repo_url": vcs_repo_url,
        "vcs_branch": vcs_branch,
        "vcs_commit_hash": vcs_commit_hash,
        "vcs_commit_url": None,
        "vcs_pull_request_url": None,
    }


def sign_payload(body_bytes: bytes, key: str) -> str:
    """Compute HMAC-SHA512 signature for task webhook."""
    return hmac.new(key.encode(), body_bytes, hashlib.sha512).hexdigest()


async def handle_run_task_call(payload: dict) -> None:
    """Handle a run task webhook delivery trigger.

    Args:
        payload: Dict with key: task_stage_result_id
    """
    import httpx

    from terrapod.config import settings
    from terrapod.http_retry import arequest_with_retry

    tsr_id_str = payload.get("task_stage_result_id", "")
    if not tsr_id_str:
        logger.warning("Incomplete run task call payload", payload=payload)
        return

    async with get_db_session() as db:
        tsr_uuid = uuid.UUID(tsr_id_str)
        tsr = await db.get(TaskStageResult, tsr_uuid)
        if tsr is None:
            logger.warning("Task stage result not found", tsr_id=tsr_id_str)
            return

        # Load task stage, run, workspace, and run task
        ts = await db.get(TaskStage, tsr.task_stage_id)
        if ts is None:
            return

        run = await db.get(Run, ts.run_id)
        if run is None:
            return

        ws = await db.get(Workspace, run.workspace_id)
        if ws is None:
            return

        from terrapod.db.models import now_utc as _utc_now

        rt = await db.get(RunTask, tsr.run_task_id) if tsr.run_task_id else None
        if rt is None:
            tsr.status = "errored"
            tsr.message = "Run task definition not found"
            tsr.finished_at = _utc_now()
            await db.flush()
            await resolve_stage(db, ts.id)
            await db.commit()
            return

        # Mark as running

        tsr.status = "running"
        tsr.started_at = _utc_now()
        await db.flush()

        # Build callback URL. Priority chain:
        #   1. public_webhook_url — set when the deployment uses a split
        #      webhook ingress (the URL external services can reach from
        #      the public internet)
        #   2. external_url — set when the management plane is itself
        #      public-reachable (no split)
        #   3. callback_base_url — last-resort fallback (also the SSO
        #      callback host; legacy path)
        # External run-task services call back from off-network, so they
        # need a base they can actually resolve and connect to.
        base = (
            settings.public_webhook_url or settings.external_url or settings.auth.callback_base_url
        ).rstrip("/")
        callback_url = f"{base}/api/v1/task-stage-results/tsr-{tsr.id}/callback"

        # A run against a speculative configuration version is always
        # plan-only (`routers/runs.py`, for TFE parity), which makes it the
        # run a PR-gating task most wants to distinguish -- so the flag comes
        # from the configuration version rather than from `run.plan_only`,
        # which is true of plan-only runs against ordinary versions too.
        #
        # `configuration_version_id` is nullable with `ondelete="SET NULL"`,
        # so a run whose version has been deleted reports False. That is the
        # honest answer: the fact is gone, and False is what it was before
        # any of this was populated.
        is_speculative = False
        if run.configuration_version_id is not None:
            cv = await db.get(ConfigurationVersion, run.configuration_version_id)
            if cv is not None:
                is_speculative = bool(cv.speculative)

        # `or ""` at the edge, as `vcs_status_dispatcher` and
        # `slack_notify_service` already do: `run_links` returns None for an
        # unset `external_url` because "no link" and "an empty link" are
        # different facts, but these two are declared string fields an
        # integration may read directly, and they have been "" since the
        # payload existed. Omitting the keys would break a reader that does
        # not expect them to vanish.
        task_payload = build_task_callback_payload(
            run_id=f"run-{run.id}",
            run_created_at=_rfc3339(run.created_at),
            run_message=run.message or "",
            run_created_by=run.created_by or "",
            run_app_url=run_links.run_url(ws.id, run.id) or "",
            workspace_id=f"ws-{ws.id}",
            workspace_name=ws.name,
            workspace_app_url=run_links.workspace_url(ws.id) or "",
            workspace_working_directory=ws.working_directory or "",
            task_name=rt.name,
            task_id=f"task-{rt.id}",
            task_result_id=f"tsr-{tsr.id}",
            task_result_enforcement_level=rt.enforcement_level,
            stage=ts.stage,
            callback_url=callback_url,
            access_token=tsr.callback_token,
            is_speculative=is_speculative,
            # Nullable, and `or None` rather than `or ""`: these are declared
            # nullable in the spec, so "absent" has a representation and an
            # empty string would be a lie about a workspace with no VCS
            # connection. The declared-string fields above keep their `or ""`.
            configuration_version_id=(
                f"cv-{run.configuration_version_id}" if run.configuration_version_id else None
            ),
            # The repo URL is the WORKSPACE's. A `Run` carries `vcs_branch`
            # and `vcs_commit_sha` but no repo URL of its own, so the three
            # VCS facts come from two different rows.
            vcs_repo_url=ws.vcs_repo_url or None,
            vcs_branch=run.vcs_branch or None,
            vcs_commit_hash=run.vcs_commit_sha or None,
        )

        hmac_key: str | None = rt.hmac_key or None

        # Send webhook
        body_bytes = json.dumps(task_payload).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if hmac_key:
            sig = sign_payload(body_bytes, hmac_key)
            # BOTH names, same signature over the same bytes (#2366). The spec
            # says `X-TFC-Task-Signature`; Terrapod has always sent
            # `X-TFE-Task-Signature`, and an integration already verifying that
            # one must keep working, so this adds rather than renames.
            headers["X-TFE-Task-Signature"] = sig
            headers[SPEC_SIGNATURE_HEADER] = sig

        try:
            # The other half of #1541. Structurally identical to the notification
            # sink and reachable by the same principal, so fixing one and leaving
            # this would just relocate the defect.
            await validate_outbound_url(rt.url)
            async with httpx.AsyncClient(timeout=30) as client:
                # Non-idempotent run-task webhook POST: retried only on
                # connection-not-sent errors, never on read-timeout/5xx — a
                # delivered run-task call must not be re-POSTed.
                resp = await arequest_with_retry(
                    client, "POST", rt.url, content=body_bytes, headers=headers
                )

            if 200 <= resp.status_code < 300:
                logger.info(
                    "Run task webhook sent",
                    tsr_id=tsr_id_str,
                    task_name=rt.name,
                    status=resp.status_code,
                )
            else:
                # Non-2xx response — mark as errored
                tsr.status = "errored"
                tsr.message = f"Webhook returned HTTP {resp.status_code}: {resp.text[:200]}"
                tsr.finished_at = _utc_now()
                await db.flush()
                await resolve_stage(db, ts.id)
                logger.warning(
                    "Run task webhook failed",
                    tsr_id=tsr_id_str,
                    status=resp.status_code,
                )

        except Exception as e:
            tsr.status = "unreachable"
            tsr.message = f"Webhook delivery failed: {str(e)[:200]}"
            tsr.finished_at = _utc_now()
            await db.flush()
            await resolve_stage(db, ts.id)
            logger.warning(
                "Run task webhook unreachable",
                tsr_id=tsr_id_str,
                error=str(e)[:200],
            )

        await db.commit()
