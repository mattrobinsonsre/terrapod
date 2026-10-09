"""API router unit tests for compliance reporting endpoints (#1704)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import runs as runs_router
from terrapod.auth import capabilities as cap
from terrapod.services import compliance_report_service


def _native_request(path: str = "/api/v1/runs/run-x/compliance-report"):
    """A stand-in Request carrying only what the handlers read: its path.

    They are called directly here rather than over HTTP, so nothing injects one
    -- and the path is not incidental. `_require_run_ws_capability` reads it to
    decide which engines the surface may serve (#1904/#1905), which is why it
    takes `request` as a required keyword rather than defaulting it. A test that
    omitted it would exercise a different code path from the one the app runs.

    Same shape as `_tfe_request` in `test_policy_checks.py`; these routes are
    native, so the path is a `/api/v1` one and the engine filter does not fire.
    """
    req = MagicMock()
    req.url.path = path
    req.query_params = {}
    return req


STAMP = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
READ_CAPS = {cap.RUN_READ}
NO_CAPS = set()


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
    async def test_show_run_compliance_report_success_envelope(self):
        db, ws, run = _setup_run(status="applied")
        mock_report = {
            "id": f"cmpl-{run.id}",
            "run-id": str(run.id),
            "workspace-id": str(ws.id),
            "verdict": "COMPLIANT",
            "run-status": "applied",
            "created-at": "2026-10-07T12:00:00Z",
            "execution-backend": "tofu",
            "is-destroy": False,
            "plan-only": False,
            "policy-checks-summary": [],
            "policy-evaluations": [],
            "security-scan": None,
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
                request=_native_request(),
                run_id=str(run.id),
                user=_user(),
                db=db,
            )

        assert response.status_code == 200
        body = response.body.decode("utf-8")
        assert '"type":"compliance-reports"' in body or '"type": "compliance-reports"' in body
        assert "COMPLIANT" in body
        assert f"cmpl-{run.id}" in body

    async def test_show_workspace_compliance_report_json_envelope(self):
        db, ws, run = _setup_run(status="applied")
        mock_workspace_report = {
            "workspace-id": str(ws.id),
            "total-runs-evaluated": 1,
            "summary": {
                "compliant": 1,
                "non-compliant": 0,
                "overridden": 0,
                "pending-review": 0,
                "compliance-rate-percent": 100.0,
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
                request=_native_request(),
                workspace_id=str(ws.id),
                limit=50,
                format="json",
                user=_user(),
                db=db,
            )

        assert response.status_code == 200
        body = response.body.decode("utf-8")
        assert (
            '"type":"workspace-compliance-reports"' in body
            or '"type": "workspace-compliance-reports"' in body
        )
        assert "compliance-rate-percent" in body

    async def test_show_workspace_compliance_report_csv(self):
        db, ws, run = _setup_run(status="applied")
        mock_workspace_report = {
            "workspace-id": str(ws.id),
            "total-runs-evaluated": 1,
            "summary": {
                "compliant": 1,
                "non-compliant": 0,
                "overridden": 0,
                "pending-review": 0,
                "compliance-rate-percent": 100.0,
            },
            "runs": [
                {
                    "run-id": str(run.id),
                    "workspace-id": str(ws.id),
                    "verdict": "COMPLIANT",
                    "run-status": "applied",
                    "created-at": "2026-10-07T12:00:00Z",
                    "execution-backend": "tofu",
                    "is-destroy": False,
                    "plan-only": False,
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
                request=_native_request(),
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

    async def test_show_run_compliance_report_forbidden_without_capability(self):
        db, ws, run = _setup_run(status="applied")

        with (
            patch.object(runs_router, "_get_run", AsyncMock(return_value=run)),
            patch.object(
                runs_router, "resolve_workspace_capabilities_for", AsyncMock(return_value=NO_CAPS)
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await runs_router.show_run_compliance_report(
                    request=_native_request(),
                    run_id=str(run.id),
                    user=_user(),
                    db=db,
                )
            assert exc_info.value.status_code == 403

    async def test_show_workspace_compliance_report_forbidden_without_capability(self):
        db, ws, run = _setup_run(status="applied")

        with (
            patch.object(runs_router, "_get_workspace", AsyncMock(return_value=ws)),
            patch.object(
                runs_router, "resolve_workspace_capabilities_for", AsyncMock(return_value=NO_CAPS)
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await runs_router.show_workspace_compliance_report(
                    request=_native_request(),
                    workspace_id=str(ws.id),
                    limit=50,
                    format="json",
                    user=_user(),
                    db=db,
                )
            assert exc_info.value.status_code == 403
