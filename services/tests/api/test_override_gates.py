"""The four gate-override routes share one capability: `run:override`.

`workspace:settings` used to be the gate; it no longer implies override. The
task-stage override is a different gate (`run-task:manage`) and is untouched.
"""

import inspect
import pathlib
import re
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api import dependencies
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers import ai_policy, policy_checks, policy_sets, run_tasks, security_scanning
from terrapod.auth import capabilities as cap
from tests.api.test_ai_policy_router import _row as _ai_row
from tests.api.test_policy_checks import _check as _pc_check
from tests.api.test_policy_checks import _setup as _pc_setup
from tests.api.test_policy_checks import _tfe_request

OVERRIDE_ONLY = {cap.RUN_READ, cap.RUN_OVERRIDE}
SETTINGS_ONLY = {cap.RUN_READ, cap.WORKSPACE_SETTINGS}


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="user@example.com",
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _db(status="planning"):
    from terrapod.db.models import Run, Workspace

    ws = MagicMock(id=uuid.uuid4(), engine="terraform")
    run = MagicMock(id=uuid.uuid4(), workspace_id=ws.id, status=status)

    async def _get(model, key):
        if model is Run and key == run.id:
            return run
        if model is Workspace and key == ws.id:
            return ws
        return None

    db = MagicMock()
    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()
    return db, run, ws


def _req(path="/api/v1/policy-checks/x"):
    req = MagicMock()
    req.url.path = path
    req.query_params = {}
    return req


async def _call_policy_checks(caps):
    db, run = _pc_setup()
    with (
        patch.object(
            policy_checks, "resolve_workspace_capabilities_for", AsyncMock(return_value=caps)
        ),
        patch.object(
            policy_checks.policy_check_service,
            "get_check",
            AsyncMock(return_value=_pc_check(run)),
        ),
        patch.object(
            policy_checks.policy_check_service, "override_check", AsyncMock(return_value=1)
        ),
        patch.object(policy_checks.run_service, "complete_plan", AsyncMock(return_value=run)),
    ):
        return await policy_checks.override_policy_check(
            request=_tfe_request(), check_id=f"polchk-opa-{run.id}", user=_user(), db=db
        )


async def _call_policy_sets(caps):
    db, run, _ = _db()
    with (
        patch.object(
            policy_sets, "resolve_workspace_capabilities_for", new=AsyncMock(return_value=caps)
        ),
        patch.object(
            policy_sets.policy_set_service,
            "override_run_policies",
            new=AsyncMock(return_value=1),
        ),
        patch.object(
            policy_sets.policy_set_service, "get_run_evaluations", new=AsyncMock(return_value=[])
        ),
        patch.object(policy_sets.run_service, "complete_plan", new=AsyncMock(return_value=run)),
    ):
        return await policy_sets.override_run_policy(run_id=f"run-{run.id}", user=_user(), db=db)


async def _call_security_scanning(caps):
    db, run, _ = _db()
    with (
        patch.object(
            security_scanning,
            "resolve_workspace_capabilities_for",
            new=AsyncMock(return_value=caps),
        ),
        patch.object(
            security_scanning.security_scan_service,
            "override_run_scan",
            new=AsyncMock(return_value=1),
        ),
        patch.object(
            security_scanning.security_scan_service,
            "get_run_scan",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            security_scanning.run_service, "complete_plan", new=AsyncMock(return_value=run)
        ),
    ):
        return await security_scanning.override_run_security_scan(
            run_id=f"run-{run.id}", user=_user(), db=db
        )


async def _call_ai_policy(caps):
    db, run, _ = _db()
    with (
        patch.object(
            ai_policy, "resolve_workspace_capabilities_for", new=AsyncMock(return_value=caps)
        ),
        patch.object(
            ai_policy.ai_policy_service,
            "override",
            new=AsyncMock(return_value=_ai_row(outcome="overridden", overridden_by="u")),
        ),
        patch.object(
            ai_policy.ai_policy_service,
            "effective_enforcement",
            new=MagicMock(return_value="mandatory"),
        ),
        patch.object(ai_policy.run_service, "complete_plan", new=AsyncMock(return_value=run)),
    ):
        return await ai_policy.override_run_ai_policy(run_id=f"run-{run.id}", user=_user(), db=db)


ROUTES = {
    "policy-check": _call_policy_checks,
    "override-policy": _call_policy_sets,
    "override-security-scan": _call_security_scanning,
    "override-ai-policy": _call_ai_policy,
}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.asyncio
async def test_run_override_alone_is_enough(route):
    resp = await ROUTES[route](OVERRIDE_ONLY)
    assert resp.status_code == 200


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.asyncio
async def test_workspace_settings_alone_is_refused(route):
    with pytest.raises(HTTPException) as exc:
        await ROUTES[route](SETTINGS_ONLY)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Requires 'run:override' capability on workspace"


def test_policy_check_permission_follows_the_shared_predicate():
    assert policy_checks._can_override(OVERRIDE_ONLY) is True
    assert policy_checks._can_override(SETTINGS_ONLY) is False


async def _call_task_stage(caps):
    db, run, ws = _db()
    ts = MagicMock(run_id=run.id)
    with (
        patch.object(run_tasks, "get_task_stage", AsyncMock(return_value=ts)),
        patch.object(run_tasks, "resolve_workspace_capabilities_for", AsyncMock(return_value=caps)),
        patch(
            "terrapod.services.run_task_service.override_stage",
            AsyncMock(side_effect=ValueError("not overridable")),
        ),
    ):
        return await run_tasks.override_task_stage(
            request=_req(), ts_id=f"ts-{uuid.uuid4()}", user=_user(), db=db
        )


@pytest.mark.asyncio
async def test_task_stage_override_still_needs_run_task_manage():
    # Reaching the service (which refuses here with 409) means the gate let it by.
    with pytest.raises(HTTPException) as exc:
        await _call_task_stage({cap.RUN_TASK_MANAGE})
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        await _call_task_stage({cap.RUN_OVERRIDE})
    assert exc.value.status_code == 403
    assert "run-task:manage" in exc.value.detail


def _override_handlers():
    """Every `actions/override*` handler on the gate routers (not task stages)."""
    out = []
    for mod in (policy_checks, policy_sets, security_scanning, ai_policy):
        src = inspect.getsource(mod)
        for m in re.finditer(
            r'@router\.post\("[^"]*actions/override[^"]*"\)\s*\nasync def (\w+)', src
        ):
            out.append((mod, m.group(1)))
    return out


def test_every_gate_override_handler_uses_the_shared_gate():
    handlers = _override_handlers()
    assert len(handlers) == 4, handlers
    for mod, name in handlers:
        body = inspect.getsource(getattr(mod, name))
        assert "WORKSPACE_SETTINGS" not in body, name
        assert "require_override(" in body, name
        assert "403" not in body, f"{name} builds its own 403 instead of require_override"


def test_the_refusal_text_has_one_producer():
    root = pathlib.Path(dependencies.__file__).resolve().parents[1]
    pattern = re.compile(r"Requires '(run:override|\{(cap\.)?RUN_OVERRIDE\})'")
    hits = [str(f.relative_to(root)) for f in root.rglob("*.py") if pattern.search(f.read_text())]
    assert hits == ["api/dependencies.py"], hits


def test_require_override_passes_the_holder_and_refuses_everyone_else():
    dependencies.require_override({cap.RUN_OVERRIDE})
    for caps in (set(), {cap.WORKSPACE_SETTINGS}, {cap.RUN_TASK_MANAGE}):
        with pytest.raises(HTTPException) as exc:
            dependencies.require_override(caps)
        assert exc.value.status_code == 403
        assert exc.value.detail == "Requires 'run:override' capability on workspace"
