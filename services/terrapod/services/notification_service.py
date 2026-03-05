"""Notification payload building, signing, and delivery providers.

Supports three destination types:
- generic: HTTP POST with optional HMAC-SHA512 signature
- slack: Slack Block Kit formatted HTTP POST
- email: SMTP via aiosmtplib
"""

import hashlib
import hmac
import json
from datetime import UTC
from email.message import EmailMessage

import httpx

from terrapod.config import settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

VALID_TRIGGERS = frozenset(
    {
        "run:created",
        "run:planning",
        "run:needs_attention",
        "run:planned",
        "run:applying",
        "run:completed",
        "run:errored",
        "run:drift_detected",
    }
)

# Maps run status to trigger event string.
# "planned" requires context (auto_apply, plan_only) so is handled in code.
STATUS_TO_TRIGGER: dict[str, str] = {
    "pending": "run:created",
    "planning": "run:planning",
    "applying": "run:applying",
    "applied": "run:completed",
    "errored": "run:errored",
}


def _rfc3339(dt) -> str:  # type: ignore[no-untyped-def]
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_run_payload(
    nc_name: str,
    run_id: str,
    run_status: str,
    run_created_at: str,
    workspace_id: str,
    workspace_name: str,
    trigger: str,
    run_message: str = "",
    run_url: str = "",
) -> dict:
    """Build a TFE V2-compatible notification payload."""
    return {
        "payload_version": 1,
        "notification_configuration_id": nc_name,
        "run_url": run_url,
        "run_id": run_id,
        "run_message": run_message,
        "run_created_at": run_created_at,
        "run_created_by": "",
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "organization_name": "default",
        "notifications": [
            {
                "message": f"Run {run_status} in workspace {workspace_name}",
                "trigger": trigger,
                "run_status": run_status,
                "run_updated_at": run_created_at,
                "run_updated_by": "",
            }
        ],
    }


def build_verification_payload(nc_name: str) -> dict:
    """Build a verification (test) payload with null fields."""
    return {
        "payload_version": 1,
        "notification_configuration_id": nc_name,
        "run_url": "",
        "run_id": None,
        "run_message": "Verification of notification configuration",
        "run_created_at": None,
        "run_created_by": None,
        "workspace_id": None,
        "workspace_name": None,
        "organization_name": "default",
        "notifications": [
            {
                "message": "Verification of " + nc_name,
                "trigger": "verification",
                "run_status": None,
                "run_updated_at": None,
                "run_updated_by": None,
            }
        ],
    }


def sign_payload(body_bytes: bytes, token: str) -> str:
    """Compute HMAC-SHA512 signature for generic webhooks."""
    return hmac.new(token.encode(), body_bytes, hashlib.sha512).hexdigest()


async def deliver_generic(
    url: str,
    payload: dict,
    token: str | None = None,
    timeout: int = 30,
) -> dict:
    """Deliver to a generic webhook endpoint.

    Returns a delivery response dict with status, body, and success flag.
    """
    body_bytes = json.dumps(payload).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        sig = sign_payload(body_bytes, token)
        headers["X-TFE-Notification-Signature"] = sig

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, content=body_bytes, headers=headers)
        return {
            "status": resp.status_code,
            "body": resp.text[:500],
            "success": 200 <= resp.status_code < 300,
        }
    except Exception as e:
        return {"status": 0, "body": str(e)[:500], "success": False}


async def deliver_slack(
    url: str,
    payload: dict,
    timeout: int = 30,
) -> dict:
    """Deliver to a Slack webhook URL using Block Kit formatting."""
    notif = payload.get("notifications", [{}])[0]
    message = notif.get("message", "Notification from Terrapod")
    trigger = notif.get("trigger", "")
    run_status = notif.get("run_status", "")
    workspace_name = payload.get("workspace_name", "")
    run_id = payload.get("run_id", "")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": message},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Trigger:*\n{trigger}"},
                {"type": "mrkdwn", "text": f"*Status:*\n{run_status or 'N/A'}"},
                {"type": "mrkdwn", "text": f"*Workspace:*\n{workspace_name or 'N/A'}"},
                {"type": "mrkdwn", "text": f"*Run:*\n{run_id or 'N/A'}"},
            ],
        },
    ]

    slack_payload = {"blocks": blocks, "text": message}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                content=json.dumps(slack_payload).encode(),
                headers={"Content-Type": "application/json"},
            )
        return {
            "status": resp.status_code,
            "body": resp.text[:500],
            "success": 200 <= resp.status_code < 300,
        }
    except Exception as e:
        return {"status": 0, "body": str(e)[:500], "success": False}


async def deliver_email(
    addresses: list[str],
    payload: dict,
) -> dict:
    """Deliver notification via SMTP email.

    Graceful no-op if SMTP is unconfigured (returns failure response).
    """
    smtp_cfg = settings.notifications.smtp
    if not smtp_cfg.host:
        return {"status": 0, "body": "SMTP not configured", "success": False}

    notif = payload.get("notifications", [{}])[0]
    message_text = notif.get("message", "Notification from Terrapod")
    workspace_name = payload.get("workspace_name", "")
    trigger = notif.get("trigger", "")
    run_id = payload.get("run_id", "")
    run_status = notif.get("run_status", "")

    subject = f"[Terrapod] {message_text}"
    body_lines = [
        message_text,
        "",
        f"Workspace: {workspace_name or 'N/A'}",
        f"Run: {run_id or 'N/A'}",
        f"Status: {run_status or 'N/A'}",
        f"Trigger: {trigger}",
    ]

    msg = EmailMessage()
    msg["From"] = smtp_cfg.from_address
    msg["To"] = ", ".join(addresses)
    msg["Subject"] = subject
    msg.set_content("\n".join(body_lines))

    try:
        import aiosmtplib

        await aiosmtplib.send(
            msg,
            hostname=smtp_cfg.host,
            port=smtp_cfg.port,
            username=smtp_cfg.username or None,
            password=smtp_cfg.password or None,
            use_tls=smtp_cfg.use_tls,
            timeout=settings.notifications.delivery_timeout_seconds,
        )
        return {"status": 250, "body": "Sent", "success": True}
    except Exception as e:
        return {"status": 0, "body": str(e)[:500], "success": False}


async def deliver_notification(
    destination_type: str,
    url: str,
    token: str | None,
    email_addresses: list[str],
    payload: dict,
) -> dict:
    """Dispatch delivery to the correct provider."""
    timeout = settings.notifications.delivery_timeout_seconds

    if destination_type == "generic":
        return await deliver_generic(url, payload, token=token, timeout=timeout)
    elif destination_type == "slack":
        return await deliver_slack(url, payload, timeout=timeout)
    elif destination_type == "email":
        return await deliver_email(email_addresses, payload)
    else:
        return {
            "status": 0,
            "body": f"Unknown destination type: {destination_type}",
            "success": False,
        }


async def record_delivery_response(
    db,  # type: ignore[no-untyped-def]
    nc_id,  # type: ignore[no-untyped-def]
    response: dict,
    max_responses: int = 10,
) -> None:
    """Append a delivery response to a notification config, capping at max."""
    from datetime import datetime

    from sqlalchemy import select

    from terrapod.db.models import NotificationConfiguration

    result = await db.execute(
        select(NotificationConfiguration).where(NotificationConfiguration.id == nc_id)
    )
    nc = result.scalar_one_or_none()
    if nc is None:
        return

    responses = list(nc.delivery_responses) if nc.delivery_responses else []
    response["delivered_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    responses.append(response)
    # Cap at max
    if len(responses) > max_responses:
        responses = responses[-max_responses:]
    nc.delivery_responses = responses
    await db.flush()
