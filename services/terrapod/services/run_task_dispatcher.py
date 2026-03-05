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

from terrapod.db.models import Run, RunTask, TaskStage, TaskStageResult, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.encryption_service import decrypt_value
from terrapod.services.run_task_service import resolve_stage

logger = get_logger(__name__)


def _rfc3339(dt) -> str:  # type: ignore[no-untyped-def]
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_task_callback_payload(
    run_id: str,
    run_status: str,
    run_created_at: str,
    workspace_id: str,
    workspace_name: str,
    task_name: str,
    stage: str,
    callback_url: str,
    access_token: str,
) -> dict:
    """Build a TFE V2-compatible run task callback payload."""
    return {
        "payload_version": 1,
        "stage": stage,
        "access_token": access_token,
        "task_result_callback_url": callback_url,
        "run_app_url": "",
        "run_id": run_id,
        "run_message": "",
        "run_created_at": run_created_at,
        "run_created_by": "",
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "workspace_app_url": "",
        "organization_name": "default",
        "task_name": task_name,
        "task_id": "",
        "is_speculative": False,
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

        from terrapod.db.models import utc_now as _utc_now

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

        # Build callback URL
        base = settings.auth.callback_base_url.rstrip("/")
        callback_url = f"{base}/api/v2/task-stage-results/tsr-{tsr.id}/callback"

        # Build payload
        task_payload = build_task_callback_payload(
            run_id=f"run-{run.id}",
            run_status=run.status,
            run_created_at=_rfc3339(run.created_at),
            workspace_id=f"ws-{ws.id}",
            workspace_name=ws.name,
            task_name=rt.name,
            stage=ts.stage,
            callback_url=callback_url,
            access_token=tsr.callback_token,
        )

        # Decrypt HMAC key if present
        hmac_key: str | None = None
        if rt.hmac_key_encrypted:
            try:
                hmac_key = decrypt_value(rt.hmac_key_encrypted)
            except Exception:
                logger.warning("Failed to decrypt HMAC key", run_task_id=str(rt.id))

        # Send webhook
        body_bytes = json.dumps(task_payload).encode()
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if hmac_key:
            sig = sign_payload(body_bytes, hmac_key)
            headers["X-TFE-Task-Signature"] = sig

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(rt.url, content=body_bytes, headers=headers)

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
