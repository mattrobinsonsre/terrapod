"""Tests for drift detection service — cycle logic and result handling."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_workspace(**overrides):
    ws = MagicMock()
    ws.id = overrides.get("id", uuid.uuid4())
    ws.name = overrides.get("name", "test-ws")
    ws.drift_detection_enabled = overrides.get("drift_detection_enabled", True)
    ws.drift_detection_interval_seconds = overrides.get("drift_detection_interval_seconds", 86400)
    ws.drift_last_checked_at = overrides.get("drift_last_checked_at", None)
    ws.drift_status = overrides.get("drift_status", "")
    ws.locked = overrides.get("locked", False)
    ws.vcs_connection_id = overrides.get("vcs_connection_id", None)
    ws.vcs_repo_url = overrides.get("vcs_repo_url", "")
    ws.auto_apply = False
    ws.terraform_version = "1.11"
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.agent_pool_id = None
    ws.owner_email = "test@example.com"
    return ws


def _mock_run(**overrides):
    run = MagicMock()
    run.id = overrides.get("id", uuid.uuid4())
    run.workspace_id = overrides.get("workspace_id", uuid.uuid4())
    run.is_drift_detection = overrides.get("is_drift_detection", True)
    run.has_changes = overrides.get("has_changes", None)
    run.status = overrides.get("status", "planned")
    run.plan_only = True
    run.auto_apply = False
    return run


class TestDriftCheckCycle:
    @patch("terrapod.services.drift_detection_service._has_state")
    @patch("terrapod.services.drift_detection_service._is_workspace_busy")
    @patch("terrapod.services.drift_detection_service._create_drift_run_non_vcs")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    async def test_skips_locked_workspace(self, mock_session, mock_create, mock_busy, mock_state):
        """Locked workspace is skipped."""
        from terrapod.services.drift_detection_service import drift_check_cycle

        ws = _mock_workspace(locked=True)

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ws]
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await drift_check_cycle()

        mock_create.assert_not_called()

    @patch("terrapod.services.drift_detection_service._has_state")
    @patch("terrapod.services.drift_detection_service._is_workspace_busy")
    @patch("terrapod.services.drift_detection_service._create_drift_run_non_vcs")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    async def test_skips_active_runs(self, mock_session, mock_create, mock_busy, mock_state):
        """Workspace with active run is skipped."""
        from terrapod.services.drift_detection_service import drift_check_cycle

        ws = _mock_workspace()
        mock_busy.return_value = True

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ws]
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await drift_check_cycle()

        mock_create.assert_not_called()

    @patch("terrapod.services.drift_detection_service._has_state")
    @patch("terrapod.services.drift_detection_service._is_workspace_busy")
    @patch("terrapod.services.drift_detection_service._create_drift_run_non_vcs")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    async def test_skips_not_yet_due(self, mock_session, mock_create, mock_busy, mock_state):
        """Workspace checked recently (within interval) is skipped."""
        from terrapod.services.drift_detection_service import drift_check_cycle

        ws = _mock_workspace(
            drift_last_checked_at=datetime.now(UTC) - timedelta(seconds=100),
            drift_detection_interval_seconds=86400,
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [ws]
        mock_db.execute.return_value = mock_result
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await drift_check_cycle()

        mock_create.assert_not_called()


class TestHandleDriftRunCompleted:
    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_maps_planned_has_changes_to_drifted(
        self, mock_run_svc, mock_session, mock_notif
    ):
        """Planned run with has_changes=True → drift_status='drifted'."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=True)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "drifted"
        mock_notif.assert_called_once()

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_maps_planned_no_changes_to_no_drift(
        self, mock_run_svc, mock_session, mock_notif
    ):
        """Planned run with has_changes=False → drift_status='no_drift'."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=False)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "no_drift"
        mock_notif.assert_not_called()

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_maps_errored_to_errored(self, mock_run_svc, mock_session, mock_notif):
        """Errored run → drift_status='errored'."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="errored")
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "errored"
        mock_notif.assert_not_called()

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_canceled_does_not_update(self, mock_run_svc, mock_session, mock_notif):
        """Canceled run → no drift_status change."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="canceled")
        ws = _mock_workspace(drift_status="no_drift")
        original_status = ws.drift_status

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == original_status

    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_unknown_has_changes_is_conservative(
        self, mock_run_svc, mock_session, mock_notif
    ):
        """Planned with has_changes=None → conservative drift."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=None)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        assert ws.drift_status == "drifted"

    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.services.drift_detection_service._enqueue_drift_notification")
    @patch("terrapod.services.drift_detection_service.get_db_session")
    @patch("terrapod.services.drift_detection_service.run_service")
    async def test_publishes_drift_status_change_event(
        self, mock_run_svc, mock_session, mock_notif, mock_publish
    ):
        """Drift status change publishes to admin, workspace-list, AND the
        per-workspace run_events channel (the last is what the workspace
        detail page listens to — without it the UI stays stale until a
        manual reload)."""
        from terrapod.services.drift_detection_service import handle_drift_run_completed

        run = _mock_run(status="planned", has_changes=True)
        ws = _mock_workspace()

        mock_db = AsyncMock()
        mock_run_svc.get_run = AsyncMock(return_value=run)
        mock_db.get.return_value = ws
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_drift_run_completed(
            {
                "run_id": str(run.id),
                "workspace_id": str(ws.id),
            }
        )

        channels = [call.args[0] for call in mock_publish.call_args_list]
        assert "tp:admin_events" in channels
        assert "tp:workspace_list_events" in channels
        # The one that fixes the detail-page-stays-stale bug
        assert f"tp:run_events:{ws.id}" in channels
        assert mock_publish.call_count == 3
