"""Tests for notification dispatcher (triggered task handler)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services.notification_dispatcher import handle_notification_delivery


class TestHandleNotificationDelivery:
    @patch("terrapod.services.notification_dispatcher.deliver_notification")
    @patch("terrapod.services.notification_dispatcher.record_delivery_response")
    @patch("terrapod.services.notification_dispatcher.get_db_session")
    async def test_delivers_to_matching_config(self, mock_db_ctx, mock_record, mock_deliver):
        """Delivery called for matching config with correct trigger."""
        ws_id = uuid.uuid4()
        run_id = uuid.uuid4()

        run = MagicMock()
        run.id = run_id
        run.workspace_id = ws_id
        run.status = "applied"
        run.message = "Test run"
        run.created_at = datetime(2026, 1, 1, tzinfo=UTC)

        ws = MagicMock()
        ws.id = ws_id
        ws.name = "test-ws"

        nc = MagicMock()
        nc.id = uuid.uuid4()
        nc.name = "test-notif"
        nc.destination_type = "generic"
        nc.url = "https://example.com/hook"
        nc.token_encrypted = None
        nc.triggers = ["run:completed"]
        nc.email_addresses = []
        nc.enabled = True

        mock_db = AsyncMock()
        mock_db.get.side_effect = lambda model, id_: run if model.__name__ == "Run" else ws
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [nc]
        mock_db.execute.return_value = mock_result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db_ctx.return_value = mock_db

        mock_deliver.return_value = {"status": 200, "body": "ok", "success": True}

        await handle_notification_delivery(
            {
                "run_id": str(run_id),
                "workspace_id": str(ws_id),
                "trigger": "run:completed",
            }
        )

        mock_deliver.assert_called_once()
        mock_record.assert_called_once()

    @patch("terrapod.services.notification_dispatcher.deliver_notification")
    @patch("terrapod.services.notification_dispatcher.record_delivery_response")
    @patch("terrapod.services.notification_dispatcher.get_db_session")
    async def test_skips_non_matching_trigger(self, mock_db_ctx, mock_record, mock_deliver):
        """Config with different trigger is skipped."""
        ws_id = uuid.uuid4()
        run_id = uuid.uuid4()

        run = MagicMock()
        run.id = run_id
        run.workspace_id = ws_id
        run.status = "applied"
        run.message = ""
        run.created_at = datetime(2026, 1, 1, tzinfo=UTC)

        ws = MagicMock()
        ws.id = ws_id
        ws.name = "test-ws"

        nc = MagicMock()
        nc.id = uuid.uuid4()
        nc.triggers = ["run:errored"]  # Won't match run:completed
        nc.enabled = True

        mock_db = AsyncMock()
        mock_db.get.side_effect = lambda model, id_: run if model.__name__ == "Run" else ws
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [nc]
        mock_db.execute.return_value = mock_result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db_ctx.return_value = mock_db

        await handle_notification_delivery(
            {
                "run_id": str(run_id),
                "workspace_id": str(ws_id),
                "trigger": "run:completed",
            }
        )

        mock_deliver.assert_not_called()

    @patch("terrapod.services.notification_dispatcher.get_db_session")
    async def test_incomplete_payload_skipped(self, mock_db_ctx):
        """Incomplete payload is silently skipped."""
        await handle_notification_delivery({"run_id": str(uuid.uuid4())})
        mock_db_ctx.assert_not_called()

    @patch("terrapod.services.notification_dispatcher.deliver_notification")
    @patch("terrapod.services.notification_dispatcher.record_delivery_response")
    @patch("terrapod.services.notification_dispatcher.get_db_session")
    async def test_run_not_found(self, mock_db_ctx, mock_record, mock_deliver):
        """Run not found results in early return."""
        mock_db = AsyncMock()
        mock_db.get.return_value = None
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db_ctx.return_value = mock_db

        await handle_notification_delivery(
            {
                "run_id": str(uuid.uuid4()),
                "workspace_id": str(uuid.uuid4()),
                "trigger": "run:completed",
            }
        )

        mock_deliver.assert_not_called()

    @patch("terrapod.services.notification_dispatcher.deliver_notification")
    @patch("terrapod.services.notification_dispatcher.record_delivery_response")
    @patch("terrapod.services.notification_dispatcher.get_db_session")
    async def test_delivery_failure_logged_not_raised(self, mock_db_ctx, mock_record, mock_deliver):
        """Delivery failure is recorded but doesn't raise."""
        ws_id = uuid.uuid4()
        run_id = uuid.uuid4()

        run = MagicMock()
        run.id = run_id
        run.workspace_id = ws_id
        run.status = "errored"
        run.message = ""
        run.created_at = datetime(2026, 1, 1, tzinfo=UTC)

        ws = MagicMock()
        ws.id = ws_id
        ws.name = "test-ws"

        nc = MagicMock()
        nc.id = uuid.uuid4()
        nc.name = "fail-notif"
        nc.destination_type = "generic"
        nc.url = "https://bad.example.com"
        nc.token_encrypted = None
        nc.triggers = ["run:errored"]
        nc.email_addresses = []
        nc.enabled = True

        mock_db = AsyncMock()
        mock_db.get.side_effect = lambda model, id_: run if model.__name__ == "Run" else ws
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [nc]
        mock_db.execute.return_value = mock_result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)
        mock_db_ctx.return_value = mock_db

        mock_deliver.return_value = {"status": 0, "body": "Connection refused", "success": False}

        # Should not raise
        await handle_notification_delivery(
            {
                "run_id": str(run_id),
                "workspace_id": str(ws_id),
                "trigger": "run:errored",
            }
        )

        mock_deliver.assert_called_once()
        mock_record.assert_called_once()
