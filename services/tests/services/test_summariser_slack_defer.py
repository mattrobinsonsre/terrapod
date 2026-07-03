"""Summariser → Slack deferral (#556): trigger mapping + fire-in-finally."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import summariser as sm


def _run(**kw):
    base = {
        "is_drift_detection": False,
        "status": "planned",
        "auto_apply": False,
        "plan_only": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_trigger_failure_analysis_is_errored():
    assert sm._slack_trigger_for(_run(), "failure_analysis") == "run:errored"


def test_trigger_plan_summary_needing_approval_is_needs_attention():
    assert sm._slack_trigger_for(_run(), "plan_summary") == "run:needs_attention"


def test_trigger_drift_is_none():
    # drift posts from its own handler (with the no-changes gate)
    assert sm._slack_trigger_for(_run(is_drift_detection=True), "plan_summary") is None


def test_trigger_auto_apply_is_none():
    assert sm._slack_trigger_for(_run(auto_apply=True), "plan_summary") is None


def test_trigger_plan_only_is_none():
    assert sm._slack_trigger_for(_run(plan_only=True), "plan_summary") is None


@pytest.mark.asyncio
async def test_handler_fires_deferred_slack_even_when_summarise_raises():
    """The deferred Slack post MUST fire in the finally even if the model errors,
    so a slow/failed model never loses the approval message."""
    stub = SimpleNamespace(id="run-1", workspace_id="ws-1")

    async def fake_summarise(payload, holder):
        holder["run"] = stub
        holder["trigger"] = "run:needs_attention"
        raise RuntimeError("model blew up")

    enq = AsyncMock()
    with (
        patch("terrapod.services.summariser._summarise_one", fake_summarise),
        patch("terrapod.services.slack_notify_service.enqueue_slack_notify", enq),
    ):
        # The error still propagates (so the scheduler logs it) — but the finally
        # must have fired the Slack post first.
        with pytest.raises(RuntimeError):
            await sm.handle_ai_plan_summary({"run_id": "run-1", "kind": "plan_summary"})

    enq.assert_awaited_once()
    assert enq.await_args.args[0] is stub
    assert enq.await_args.args[1] == "run:needs_attention"
    assert enq.await_args.kwargs.get("_from_summariser") is True


@pytest.mark.asyncio
async def test_handler_no_slack_when_no_trigger_captured():
    """If the summariser bailed before loading the run (no trigger), no post."""

    async def fake_summarise(payload, holder):
        return  # never populates holder

    enq = AsyncMock()
    with (
        patch("terrapod.services.summariser._summarise_one", fake_summarise),
        patch("terrapod.services.slack_notify_service.enqueue_slack_notify", enq),
    ):
        await sm.handle_ai_plan_summary({"run_id": "run-1", "kind": "plan_summary"})

    enq.assert_not_awaited()
