"""`latest-run` carries the run's own `actions` and `run-kind`.

A client cannot rebuild the answer from what the workspace document publishes
(`has_changes`, the resolved auto-apply mode and `plan_finished_at` are all
absent), so the document mirrors the answer rather than its inputs. It is the
same `run_actions` the run document uses, under the same wire names, on both
surfaces.

Real ORM rows, as in `test_latest_run_phase_starts.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from terrapod.api.routers import tfe_v2
from terrapod.db.models import Run, Workspace
from terrapod.services.run_service import run_actions

T = datetime(2026, 1, 1, tzinfo=UTC)

CASES = {
    "planned-unknown-changes": ("planned", {"has_changes": None}),
    "planned-with-changes": ("planned", {"has_changes": True}),
    "planned-no-changes": ("planned", {"has_changes": False}),
    "planned-always-auto-apply": ("planned", {"auto_apply_mode": "always"}),
    "planned-plan-only": ("planned", {"plan_only": True}),
    "held-apply-capable": ("planning", {"plan_finished_at": T}),
    "held-plan-only": ("planning", {"plan_finished_at": T, "plan_only": True}),
    "planning": ("planning", {}),
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
    acts = run_actions(run)
    assert _latest(run, tfe=tfe)["actions"] == {
        "is-confirmable": acts.confirmable,
        "is-discardable": acts.discardable,
        "is-cancelable": acts.cancelable,
        "is-retryable": acts.retryable,
    }


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
