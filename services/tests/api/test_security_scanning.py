"""Tests for the security-scanning router — run-lifecycle + runner protocol (#1036).

Structural twin of ``test_policy_runner_endpoints.py``. Four endpoints:

- ``GET  /runs/{id}/security-scan``                  (workspace read)
- ``POST /runs/{id}/actions/override-security-scan`` (workspace admin, re-drive)
- ``GET  /runs/{id}/security-scan-config``           (runner token)
- ``POST /runs/{id}/security-scan-results``          (runner token)

These exercise the auth boundary (a leaked runner token for run A can't drive
scan state on run B), the RBAC gates on the read/override endpoints, the runner
result validation, and — the security-critical bit — that the persisted
enforcement/threshold are re-resolved **server-side from the workspace**, never
trusted from the runner body.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import security_scanning as router
from terrapod.auth import capabilities as cap


def _user(*, method: str = "session", run_id: str | None = None) -> AuthenticatedUser:
    return AuthenticatedUser(
        email="user@terrapod",
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method=method,
        run_id=run_id,
    )


def _ws(**kw):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.name = "smoke"
    m.security_scan_enforcement = kw.get("enforcement", "advisory")
    m.security_scan_severity_threshold = kw.get("threshold", "high")
    m.security_scan_engine = kw.get("engine", "checkov")
    m.security_scan_skip_rules = kw.get("skip_rules", [])
    return m


def _run(ws, **kw):
    m = MagicMock()
    m.id = kw.get("id", uuid.uuid4())
    m.workspace_id = ws.id
    m.status = kw.get("status", "planning")
    m.plan_only = kw.get("plan_only", False)
    return m


def _mock_db(run, ws):
    db = MagicMock()

    async def _get(model, key):
        from terrapod.db.models import Run, Workspace

        if model is Run:
            return run if key == run.id else None
        if model is Workspace:
            return ws if key == ws.id else None
        return None

    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    return db


# ── GET /security-scan (workspace read) ───────────────────────────────


@pytest.mark.asyncio
async def test_get_scan_requires_read_capability() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    with patch.object(
        router, "resolve_workspace_capabilities_for", new=AsyncMock(return_value=set())
    ):
        with pytest.raises(HTTPException) as exc:
            await router.get_run_security_scan(run_id=f"run-{run.id}", user=_user(), db=db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_get_scan_404_on_unknown_run() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    with pytest.raises(HTTPException) as exc:
        await router.get_run_security_scan(run_id=f"run-{uuid.uuid4()}", user=_user(), db=db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_scan_returns_result_and_summary() -> None:
    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    scan = MagicMock(
        id=uuid.uuid4(),
        engine="checkov",
        enforcement_level="advisory",
        severity_threshold="high",
        outcome="failed",
        findings=[{"rule_id": "CKV_AWS_18"}],
        summary={"total": 1, "blocking": 0},
        error=None,
        overridden_by=None,
        overridden_at=None,
        created_at=None,
    )
    with (
        patch.object(
            router, "resolve_workspace_capabilities_for", new=AsyncMock(return_value={cap.RUN_READ})
        ),
        patch.object(
            router.security_scan_service, "get_run_scan", new=AsyncMock(return_value=scan)
        ),
        patch.object(
            router.security_scan_service,
            "run_scan_summary",
            new=AsyncMock(return_value={"status": "advisory-failed", "total": 1}),
        ),
    ):
        resp = await router.get_run_security_scan(run_id=f"run-{run.id}", user=_user(), db=db)
    body = json.loads(resp.body)
    assert body["data"]["attributes"]["outcome"] == "failed"
    assert body["data"]["attributes"]["enforcement-level"] == "advisory"
    assert body["meta"]["summary"]["status"] == "advisory-failed"


# ── POST /actions/override-security-scan (workspace admin) ─────────────


@pytest.mark.asyncio
async def test_override_requires_settings_capability() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    with patch.object(
        router, "resolve_workspace_capabilities_for", new=AsyncMock(return_value={cap.RUN_READ})
    ):
        with pytest.raises(HTTPException) as exc:
            await router.override_run_security_scan(run_id=f"run-{run.id}", user=_user(), db=db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_override_redrives_planning_run() -> None:
    ws = _ws(enforcement="enforced")
    run = _run(ws, status="planning")

    async def _complete(_db, r):
        r.status = "applied"
        return r

    db = _mock_db(run, ws)
    with (
        patch.object(
            router,
            "resolve_workspace_capabilities_for",
            new=AsyncMock(return_value={cap.WORKSPACE_SETTINGS}),
        ),
        patch.object(
            router.security_scan_service, "override_run_scan", new=AsyncMock(return_value=1)
        ),
        patch.object(router.run_service, "complete_plan", new=AsyncMock(side_effect=_complete)),
        patch.object(
            router.security_scan_service, "get_run_scan", new=AsyncMock(return_value=None)
        ),
    ):
        resp = await router.override_run_security_scan(run_id=f"run-{run.id}", user=_user(), db=db)
    body = json.loads(resp.body)
    assert body["meta"]["overridden"] == 1
    assert body["meta"]["run-status"] == "applied"  # re-driven past the gate


# ── GET /security-scan-config (runner token) ──────────────────────────


@pytest.mark.asyncio
async def test_config_rejects_non_runner_token() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    with pytest.raises(HTTPException) as exc:
        await router.get_security_scan_config(
            run_id=f"run-{run.id}", user=_user(method="api_token"), db=db
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_config_rejects_token_for_wrong_run() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    user = _user(method="runner_token", run_id="run-different")
    with pytest.raises(HTTPException) as exc:
        await router.get_security_scan_config(run_id=f"run-{run.id}", user=user, db=db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_config_returns_workspace_scan_config() -> None:
    ws = _ws(enforcement="enforced", engine="both", threshold="critical", skip_rules=["CKV_AWS_1"])
    run = _run(ws)
    db = _mock_db(run, ws)
    run_id = f"run-{run.id}"
    user = _user(method="runner_token", run_id=run_id)
    resp = await router.get_security_scan_config(run_id=run_id, user=user, db=db)
    body = json.loads(resp.body)
    assert body == {
        "enabled": True,
        "enforcement_level": "enforced",
        "engine": "both",
        "severity_threshold": "critical",
        "skip_rules": ["CKV_AWS_1"],
    }


# ── POST /security-scan-results (runner token) ────────────────────────


@pytest.mark.asyncio
async def test_results_rejects_non_runner_token() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    with pytest.raises(HTTPException) as exc:
        await router.post_security_scan_results(
            run_id=f"run-{run.id}",
            body={"outcome": "passed", "findings": []},
            user=_user(method="api_token"),
            db=db,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_results_rejects_unknown_outcome() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    run_id = f"run-{run.id}"
    user = _user(method="runner_token", run_id=run_id)
    with pytest.raises(HTTPException) as exc:
        await router.post_security_scan_results(
            run_id=run_id, body={"outcome": "bogus", "findings": []}, user=user, db=db
        )
    assert exc.value.status_code == 422
    assert "outcome" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_results_rejects_non_list_findings() -> None:
    from fastapi import HTTPException

    ws = _ws()
    run = _run(ws)
    db = _mock_db(run, ws)
    run_id = f"run-{run.id}"
    user = _user(method="runner_token", run_id=run_id)
    with pytest.raises(HTTPException) as exc:
        await router.post_security_scan_results(
            run_id=run_id, body={"outcome": "passed", "findings": "nope"}, user=user, db=db
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_results_reresolves_enforcement_from_workspace_not_runner() -> None:
    """The runner cannot post ``advisory`` to slip past an ``enforced`` gate —
    the persisted enforcement/threshold come from the workspace row, server-side."""
    ws = _ws(enforcement="enforced", threshold="critical")
    run = _run(ws)
    db = _mock_db(run, ws)
    run_id = f"run-{run.id}"
    user = _user(method="runner_token", run_id=run_id)

    captured: dict = {}

    async def _record(_db, **kw):
        captured.update(kw)

    body = {
        "engine": "checkov",
        "outcome": "failed",
        "findings": [{"rule_id": "CKV_AWS_18"}],
        "summary": {"total": 1, "blocking": 1},
        # a compromised runner claiming it was only advisory:
        "enforcement_level": "advisory",
        "severity_threshold": "low",
    }
    with patch.object(
        router.security_scan_service, "record_scan_result", new=AsyncMock(side_effect=_record)
    ):
        resp = await router.post_security_scan_results(run_id=run_id, body=body, user=user, db=db)
    assert resp.status_code == 201
    # server-side authoritative values, NOT the runner's claimed ones
    assert captured["enforcement_level"] == "enforced"
    assert captured["severity_threshold"] == "critical"
    assert captured["outcome"] == "failed"
