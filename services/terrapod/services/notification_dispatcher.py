"""Triggered task handler for notification delivery.

Registered with the distributed scheduler as a trigger handler.
Receives {run_id, workspace_id, trigger} payloads, queries matching
notification configs, and delivers to each.
"""

import uuid

from sqlalchemy import select

from terrapod.config import settings
from terrapod.db.models import NotificationConfiguration, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services.notification_service import (
    build_run_payload,
    deliver_notification,
    record_delivery_response,
)

logger = get_logger(__name__)


async def handle_notification_delivery(payload: dict) -> None:
    """Handle a notification delivery trigger.

    Args:
        payload: Dict with keys: run_id, workspace_id, trigger
    """
    run_id_str = payload.get("run_id", "")
    workspace_id_str = payload.get("workspace_id", "")
    trigger = payload.get("trigger", "")

    if not run_id_str or not workspace_id_str or not trigger:
        logger.warning("Incomplete notification payload", payload=payload)
        return

    async with get_db_session() as db:
        # Load the run and workspace
        run_uuid = uuid.UUID(run_id_str)
        ws_uuid = uuid.UUID(workspace_id_str)

        run = await db.get(Run, run_uuid)
        ws = await db.get(Workspace, ws_uuid)

        if run is None or ws is None:
            logger.warning(
                "Run or workspace not found for notification",
                run_id=run_id_str,
                workspace_id=workspace_id_str,
            )
            return

        # Query enabled notification configs for this workspace
        result = await db.execute(
            select(NotificationConfiguration).where(
                NotificationConfiguration.workspace_id == ws_uuid,
                NotificationConfiguration.enabled.is_(True),
            )
        )
        configs = list(result.scalars().all())

        if not configs:
            return

        max_responses = settings.notifications.max_delivery_responses

        for nc in configs:
            # Check if this config's triggers include the event
            nc_triggers = nc.triggers if nc.triggers else []
            if trigger not in nc_triggers:
                continue

            token: str | None = nc.token or None

            # Build payload
            from terrapod.services.notification_service import _rfc3339

            notif_payload = build_run_payload(
                nc_name=nc.name,
                run_id=f"run-{run.id}",
                run_status=run.status,
                run_created_at=_rfc3339(run.created_at),
                workspace_id=f"ws-{ws.id}",
                workspace_name=ws.name,
                trigger=trigger,
                run_message=run.message,
            )

            # Deliver
            email_addresses = nc.email_addresses if nc.email_addresses else []
            response = await deliver_notification(
                destination_type=nc.destination_type,
                url=nc.url,
                token=token,
                email_addresses=email_addresses,
                payload=notif_payload,
            )

            # Record delivery response
            await record_delivery_response(db, nc.id, response, max_responses)

            if response.get("success"):
                logger.info(
                    "Notification delivered",
                    nc_name=nc.name,
                    trigger=trigger,
                    status=response.get("status"),
                )
            else:
                logger.warning(
                    "Notification delivery failed",
                    nc_name=nc.name,
                    trigger=trigger,
                    status=response.get("status"),
                    body=response.get("body", "")[:200],
                )

        await db.commit()
