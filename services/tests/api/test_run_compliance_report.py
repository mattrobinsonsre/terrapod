"""Unit tests for the Run Compliance Reporting API endpoints (#1704)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import runs as runs_router
from terrapod.auth import capabilities as cap
from terrapod.services import compliance_report_service

STAMP = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
READ_CAPS = {cap.RUN_READ}


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="auditor@example.com",
        display_name="Auditor",
        roles=["auditor"],
        provider_name="local",
        auth_method="session",
    )


def _setup_run(status="planned"):
    ws_id = uuid.uuid4()
    ws = MagicMock(id=ws_id, name="prod-workspace")
    run_id = uuid.uuid4()
    run = MagicMock(
        id=run_id,
        workspace_id=ws_id,
        status=status,
        created_at=STAMP,
        execution_backend="tofu",
        is_destroy=False,
        plan_only=False,
    )

    db = MagicMock()

    async def _get(model, key):
        from terrapod.db.models import Run, Workspace

        if model is Run and str(key) == str(run_id):
            return run
        if model is Workspace and str(key) == str(ws_id):
            return ws
        return None

    db.get = AsyncMock(side_effect=_get)
    return db, ws, run


class TestRunComplianceReportAPI:
    async def test_show_run_compliance_report_success(self):
        db, ws, run = _setup_run(status="applied")
        mock_report = {
            "id": f"cmpl-{run.id}",
            "run_id": str(run.id),
            "workspace_id": str(ws.id),
            "verdict": "COMPLIANT",
            "run_status": "applied",
            "created_at": "2026-09-28T12:00:00Z",
            "execution_backend": "tofu",
            "is_destroy": False,
            "plan_only": False,
            "policy_checks_summary": [],
            "policy_evaluations": [],
            "security_scan": None,
        }

        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch.object(
                runs_router, "resolve_workspace_capabilities_for", AsyncMock(return_value=READ_CAPS)
            ),
            patch.object(
                compliance_report_service,
                "generate_run_compliance_report",
                AsyncMock(return_value=mock_report),
            ),
        ):
            response = await runs_router.show_run_compliance_report(
                run_id=str(run.id),
                user=_user(),
                db=db,
            )

        assert response.status_code == 200
        body = response.body.decode("utf-8")
        assert "COMPLIANT" in body
        assert f"cmpl-{run.id}" in body

    async def test_show_workspace_compliance_report_json(self):
        db, ws, run = _setup_run(status="applied")
        mock_workspace_report = {
            "workspace_id": str(ws.id),
            "total_runs_evaluated": 1,
            "summary": {
                "compliant": 1,
                "non_compliant": 0,
                "overridden": 0,
                "pending_review": 0,
                "compliance_rate_percent": 100.0,
            },
            "runs": [],
        }

        with (
            patch.object(runs_router, "_get_workspace", AsyncMock(return_value=ws)),
            patch.object(
                runs_router, "resolve_workspace_capabilities_for", AsyncMock(return_value=READ_CAPS)
            ),
            patch.object(
                compliance_report_service,
                "generate_workspace_compliance_report",
                AsyncMock(return_value=mock_workspace_report),
            ),
        ):
            response = await runs_router.show_workspace_compliance_report(
                workspace_id=str(ws.id),
                limit=50,
                format="json",
                user=_user(),
                db=db,
            )

        assert response.status_code == 200
        body = response.body.decode("utf-8")
        assert "compliance_rate_percent" in body

    async def test_show_workspace_compliance_report_csv(self):
        db, ws, run = _setup_run(status="applied")
        mock_workspace_report = {
            "workspace_id": str(ws.id),
            "total_runs_evaluated": 1,
            "summary": {
                "compliant": 1,
                "non_compliant": 0,
                "overridden": 0,
                "pending_review": 0,
                "compliance_rate_percent": 100.0,
            },
            "runs": [
                {
                    "run_id": str(run.id),
                    "workspace_id": str(ws.id),
                    "verdict": "COMPLIANT",
                    "run_status": "applied",
                    "created_at": "2026-09-28T12:00:00Z",
                    "execution_backend": "tofu",
                    "is_destroy": False,
                    "plan_only": False,
                }
            ],
        }

        with (
            patch.object(runs_router, "_get_workspace", AsyncMock(return_value=ws)),
            patch.object(
                runs_router, "resolve_workspace_capabilities_for", AsyncMock(return_value=READ_CAPS)
            ),
            patch.object(
                compliance_report_service,
                "generate_workspace_compliance_report",
                AsyncMock(return_value=mock_workspace_report),
            ),
        ):
            response = await runs_router.show_workspace_compliance_report(
                workspace_id=str(ws.id),
                limit=50,
                format="csv",
                user=_user(),
                db=db,
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        csv_text = response.body.decode("utf-8")
        assert "run_id,workspace_id,verdict" in csv_text
        assert "COMPLIANT" in csv_text
