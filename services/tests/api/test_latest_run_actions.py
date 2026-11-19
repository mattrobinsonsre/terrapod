"""`latest-run` mirrors the run document's `actions` and `run-kind`, and carries `is-destroy`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.routers import runs, tfe_v2
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.models import Run, Workspace
from tests.api.test_workspaces import (
    _AUTH,
    _BASE,
    _make_app,
    _mock_workspace,
    _no_inert_vars,
    _user,
)

T = datetime(2026, 1, 1, tzinfo=UTC)

CASES = {
    "planned-unknown-changes": ("planned", {"has_changes": None}),
    "planned-with-changes": ("planned", {"has_changes": True}),
    "planned-no-changes": ("planned", {"has_changes": False}),
    "planned-always-auto-apply": ("planned", {"auto_apply_mode": "always"}),
    "planned-plan-only": ("planned", {"plan_only": True}),
    "planned-never-auto-apply": ("planned", {"auto_apply_mode": "never"}),
    "planned-create-auto-apply": ("planned", {"auto_apply_mode": "create"}),
    "planned-create-update-auto-apply": ("planned", {"auto_apply_mode": "create_update"}),
    "held-apply-capable": ("planning", {"plan_finished_at": T}),
    "held-plan-only": ("planning", {"plan_finished_at": T, "plan_only": True}),
    "planning": ("planning", {}),
    "pending": ("pending", {}),
    "queued": ("queued", {}),
    "confirmed": ("confirmed", {}),
    "applying": ("applying", {}),
    "applied": ("applied", {}),
    "errored": ("errored", {}),
    "discarded": ("discarded", {}),
    "canceled": ("canceled", {}),
    "configuring": ("configuring", {}),
}


def _run(status: str, **cols) -> Run:
    cols.setdefault("plan_only", False)
    return Run(id=uuid.uuid4(), status=status, created_at=T, **cols)


def _latest(run: Run | None, *, tfe: bool) -> dict | None:
    ws = Workspace(id=uuid.uuid4(), name="ws", created_at=T, updated_at=T)
    doc = tfe_v2._workspace_json(ws, latest_run=run, tfe=tfe)
    return doc["data"]["attributes"]["latest-run"]


@pytest.mark.parametrize("tfe", [False, True], ids=["native", "tfe"])
@pytest.mark.parametrize("status,cols", CASES.values(), ids=CASES.keys())
def test_actions_are_the_run_documents_answer(status, cols, tfe):
    run = _run(status, **cols)
    run_doc = runs._run_json(run, engine="terraform", tfe=tfe)["data"]["attributes"]
    assert _latest(run, tfe=tfe)["actions"] == run_doc["actions"]


@pytest.mark.parametrize("tfe", [False, True], ids=["native", "tfe"])
@pytest.mark.parametrize(
    "flags,kind",
    [
        ({"is_configure": True, "configure_after_apply": False}, "configure"),
        ({"is_configure": True, "configure_after_apply": True}, "full"),
        ({"is_configure": False, "configure_after_apply": False}, "plan_apply"),
        ({"is_configure": False, "configure_after_apply": True}, "plan_apply"),
    ],
)
def test_run_kind(flags, kind, tfe):
    assert _latest(_run("errored", **flags), tfe=tfe)["run-kind"] == kind


@pytest.mark.parametrize("tfe", [False, True], ids=["native", "tfe"])
def test_no_run_means_no_latest_run(tfe):
    assert _latest(None, tfe=tfe) is None


@pytest.mark.parametrize("tfe", [False, True], ids=["native", "tfe"])
@pytest.mark.parametrize("is_destroy", [True, False])
def test_is_destroy_is_the_runs_own_flag(is_destroy, tfe):
    run = _run("planned", is_destroy=is_destroy)
    latest = _latest(run, tfe=tfe)
    assert latest["is-destroy"] is is_destroy
    assert latest["run-kind"] == "plan_apply"
    assert latest["plan-only"] is False


@pytest.mark.parametrize(
    "path,kind",
    [
        ("/api/v1/workspaces", "list"),
        ("/api/v2/organizations/default/workspaces", "list"),
        ("/api/v1/workspaces/ws-{id}", "show"),
        ("/api/v2/workspaces/ws-{id}", "show"),
    ],
)
@pytest.mark.parametrize("is_destroy", [True, False])
async def test_is_destroy_on_every_workspace_read(path, kind, is_destroy):
    ws = _mock_workspace(name="d-ws")
    run = _run("planned", is_destroy=is_destroy, workspace_id=ws.id)
    ws_result = MagicMock()
    ws_result.scalars.return_value.all.return_value = [ws]
    ws_result.scalar_one_or_none.return_value = ws
    run_result = MagicMock()
    run_result.scalars.return_value.all.return_value = [run]
    run_result.scalar_one_or_none.return_value = run
    app, db = _make_app(_user(roles=["admin"]))
    db.execute.side_effect = [ws_result, run_result, _no_inert_vars()]
    with (
        patch("terrapod.engines.known_engines", return_value=["terraform"]),
        patch(
            "terrapod.api.routers.workspace_extensions.resolve_workspace_capabilities_for",
            AsyncMock(return_value=caps_for_level("read")),
        ),
        patch(
            "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
            AsyncMock(return_value=caps_for_level("read")),
        ),
        patch(
            "terrapod.api.routers.tfe_v2._resolve_live_pools", AsyncMock(return_value=frozenset())
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(path.format(id=ws.id), headers=_AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    attrs = (data[0] if kind == "list" else data)["attributes"]
    assert attrs["latest-run"]["is-destroy"] is is_destroy
