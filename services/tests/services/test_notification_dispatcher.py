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
        nc.token = None
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
        nc.token = None
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


class TestThePayloadSaysWhereAndWho:
    """#1706: generic and email notifications always sent an empty `run_url`
    and `run_created_by` -- the two fields that make an alert actionable. The
    dispatcher, the only production caller, never passed either."""

    async def _deliver(self, *, created_by: str, external_url: str) -> dict:
        ws_id, run_id = uuid.uuid4(), uuid.uuid4()
        run = MagicMock()
        run.id = run_id
        run.workspace_id = ws_id
        run.status = "applied"
        run.message = "Test run"
        run.created_by = created_by
        run.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        ws = MagicMock()
        ws.id = ws_id
        ws.name = "test-ws"
        nc = MagicMock()
        nc.id = uuid.uuid4()
        nc.name = "test-notif"
        nc.destination_type = "generic"
        nc.url = "https://example.com/hook"
        nc.token = None
        nc.triggers = ["run:completed"]
        nc.email_addresses = []
        nc.enabled = True

        mock_db = AsyncMock()
        mock_db.get.side_effect = lambda model, id_: run if model.__name__ == "Run" else ws
        result = MagicMock()
        result.scalars.return_value.all.return_value = [nc]
        mock_db.execute.return_value = result
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("terrapod.services.notification_dispatcher.get_db_session", return_value=mock_db),
            patch("terrapod.services.notification_dispatcher.record_delivery_response"),
            patch("terrapod.services.notification_dispatcher.deliver_notification") as deliver,
            patch("terrapod.config.settings.external_url", external_url),
        ):
            deliver.return_value = {"status": 200, "body": "ok", "success": True}
            await handle_notification_delivery(
                {"run_id": str(run_id), "workspace_id": str(ws_id), "trigger": "run:completed"}
            )
        payload = deliver.call_args.kwargs.get("payload")
        if payload is None:
            payload = next(a for a in deliver.call_args.args if isinstance(a, dict))
        return {"payload": payload, "ws_id": ws_id, "run_id": run_id}

    async def test_run_url_links_to_the_run_page(self):
        out = await self._deliver(
            created_by="ops@example.com", external_url="https://tp.example.com/"
        )
        assert out["payload"]["run_url"] == (
            f"https://tp.example.com/workspaces/ws-{out['ws_id']}/runs/run-{out['run_id']}"
        )

    async def test_run_created_by_names_who_queued_it(self):
        out = await self._deliver(
            created_by="ops@example.com", external_url="https://tp.example.com"
        )
        assert out["payload"]["run_created_by"] == "ops@example.com"
        # Who changed the status isn't recorded, so it is left empty rather than
        # attributing a confirm or cancel to the creator.
        assert out["payload"]["notifications"][0]["run_updated_by"] == ""

    async def test_no_external_url_leaves_run_url_empty(self):
        # There is no honest link without the host people reach the UI on, and
        # an empty `run_url` is what receivers already handle.
        out = await self._deliver(created_by="ops@example.com", external_url="")
        assert out["payload"]["run_url"] == ""


class TestEmailCarriesTheLinkAndActor:
    """#1706: email notifications showed neither the run link nor who started it."""

    async def _send(self, payload: dict) -> str:
        from terrapod.services import notification_service

        sent = {}

        async def _capture(msg, **_):
            sent["body"] = msg.get_content()

        with (
            patch.object(
                notification_service.settings.notifications.smtp, "host", "smtp.example.com"
            ),
            patch("aiosmtplib.send", side_effect=_capture),
        ):
            await notification_service.deliver_email(["ops@example.com"], payload)
        return sent["body"]

    def _payload(self, **extra) -> dict:
        return {
            "run_id": "run-1",
            "workspace_name": "prod",
            "notifications": [
                {"message": "Run applied", "trigger": "run:completed", "run_status": "applied"}
            ],
            **extra,
        }

    async def test_the_body_links_to_the_run_and_names_who_started_it(self):
        body = await self._send(
            self._payload(
                run_url="https://tp.example.com/workspaces/ws-1/runs/run-1",
                run_created_by="ops@example.com",
            )
        )
        assert "View the run: https://tp.example.com/workspaces/ws-1/runs/run-1" in body
        assert "Started by: ops@example.com" in body

    async def test_unknown_values_are_left_out_rather_than_shown_empty(self):
        body = await self._send(self._payload(run_url="", run_created_by=""))
        assert "View the run" not in body
        assert "Started by" not in body
