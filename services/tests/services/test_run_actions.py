"""`run_actions` is the one answer to what can be done to a run, on real `Run` rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from terrapod.db.models import Run
from terrapod.services.run_service import RunActions, run_actions

T = datetime(2026, 1, 1, tzinfo=UTC)


def _run(status: str, **cols) -> Run:
    cols.setdefault("plan_only", False)
    return Run(id=uuid.uuid4(), status=status, created_at=T, **cols)


def _acts(run: Run) -> tuple[bool, bool, bool, bool]:
    a = run_actions(run)
    return a.confirmable, a.discardable, a.cancelable, a.retryable


class TestPlanned:
    def test_unknown_changes_can_be_confirmed_and_discarded(self):
        assert _acts(_run("planned", has_changes=None)) == (True, True, False, False)

    def test_a_plan_with_changes_can_be_confirmed_and_discarded(self):
        assert _acts(_run("planned", has_changes=True)) == (True, True, False, False)

    def test_a_plan_with_no_changes_offers_neither(self):
        assert _acts(_run("planned", has_changes=False)) == (False, False, False, False)

    def test_always_auto_apply_is_discardable_but_not_confirmable(self):
        run = _run("planned", auto_apply_mode="always")
        assert _acts(run) == (False, True, False, False)

    def test_a_plan_only_run_is_only_retryable(self):
        run = _run("planned", plan_only=True)
        assert _acts(run) == (False, False, False, True)


class TestHeldAtAGate:
    def test_an_apply_capable_hold_is_discardable_and_cancelable(self):
        run = _run("planning", plan_finished_at=T)
        assert _acts(run) == (False, True, True, False)

    def test_a_plan_only_hold_is_cancelable_not_discardable(self):
        run = _run("planning", plan_finished_at=T, plan_only=True)
        assert _acts(run) == (False, False, True, False)

    def test_a_plan_still_running_is_only_cancelable(self):
        assert _acts(_run("planning")) == (False, False, True, False)


@pytest.mark.parametrize("status", ["applied", "errored", "discarded", "canceled"])
def test_a_terminal_run_is_only_retryable(status):
    assert _acts(_run(status)) == (False, False, False, True)


def test_a_configuring_run_is_cancelable():
    assert _acts(_run("configuring")) == (False, False, True, False)


def test_the_result_is_a_frozen_value():
    with pytest.raises(AttributeError):
        run_actions(_run("planned")).confirmable = False  # type: ignore[misc]
    assert RunActions(True, False, False, True).retryable is True
