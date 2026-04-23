"""Tests for VCS commit-status resolution — has-changes descriptions."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.vcs_status_dispatcher import (
    _build_comment_body,
    _resolve_status,
)


class TestResolveStatusPlanned:
    """The `planned` status description depends on plan_only and has_changes."""

    def test_plan_only_with_changes(self):
        gh, gl, desc = _resolve_status("planned", plan_only=True, has_changes=True)
        assert gh == "success"
        assert gl == "success"
        assert desc == "Has changes"

    def test_plan_only_no_changes(self):
        gh, gl, desc = _resolve_status("planned", plan_only=True, has_changes=False)
        assert gh == "success"
        assert gl == "success"
        assert desc == "No changes"

    def test_plan_only_unknown_changes_falls_back(self):
        """When has_changes is None the description stays generic."""
        gh, gl, desc = _resolve_status("planned", plan_only=True, has_changes=None)
        assert gh == "success"
        assert desc == "Plan finished"

    def test_apply_run_with_changes_awaiting_confirmation(self):
        gh, gl, desc = _resolve_status("planned", plan_only=False, has_changes=True)
        assert gh == "pending"
        assert gl == "running"
        assert desc == "Has changes, awaiting confirmation"

    def test_apply_run_no_changes_is_success_not_pending(self):
        """No changes = nothing to apply = nothing to confirm. Success, not pending."""
        gh, gl, desc = _resolve_status("planned", plan_only=False, has_changes=False)
        assert gh == "success"
        assert gl == "success"
        assert desc == "No changes"

    def test_apply_run_unknown_changes_generic(self):
        _, _, desc = _resolve_status("planned", plan_only=False, has_changes=None)
        assert desc == "Plan complete, awaiting confirmation"


class TestResolveStatusNonPlanned:
    """Other statuses are unaffected by has_changes."""

    def test_applied(self):
        gh, gl, desc = _resolve_status("applied", plan_only=False, has_changes=True)
        assert gh == "success"
        assert desc == "Apply complete"

    def test_errored(self):
        gh, _, desc = _resolve_status("errored", plan_only=True, has_changes=None)
        assert gh == "failure"
        assert desc == "Run failed"

    def test_queued(self):
        gh, _, desc = _resolve_status("queued", plan_only=False, has_changes=None)
        assert gh == "pending"
        assert desc == "Waiting for runner"


class TestBuildCommentBody:
    """The PR comment body should not duplicate has-changes info — the
    description line already carries it; a second sentence saying the
    same thing is noise."""

    def test_has_changes_description_and_no_duplicate_sentence(self):
        body = _build_comment_body(
            workspace_name="sls-prod-us1",
            workspace_id=str(uuid.uuid4()),
            run_id="run-abc",
            run_status="planned",
            plan_only=True,
            has_changes=True,
            run_url="https://terrapod.example/workspaces/x/runs/y",
        )
        assert "Has changes" in body
        assert "review in Terrapod" not in body  # old redundant line
        assert "No changes detected" not in body

    def test_no_changes_description_and_no_duplicate_sentence(self):
        body = _build_comment_body(
            workspace_name="sls-prod-us1",
            workspace_id=str(uuid.uuid4()),
            run_id="run-abc",
            run_status="planned",
            plan_only=True,
            has_changes=False,
            run_url="https://terrapod.example/x",
        )
        assert "No changes" in body
        assert "No changes detected" not in body  # old redundant line


class TestEnqueueVcsStatus:
    """_enqueue_vcs_status must carry has_changes in the payload (closing
    the commit-vs-enqueue race) and must skip drift runs."""

    @pytest.mark.asyncio
    async def test_has_changes_put_in_payload(self):
        from terrapod.services.run_service import _enqueue_vcs_status

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.has_changes = True
        run.is_drift_detection = False

        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as mock_enq:
            await _enqueue_vcs_status(run, "planned")

        mock_enq.assert_awaited_once()
        # enqueue_trigger(name, payload, dedup_key=..., dedup_ttl=...)
        _, payload = mock_enq.await_args.args
        assert payload["has_changes"] is True
        assert payload["target_status"] == "planned"

    @pytest.mark.asyncio
    async def test_has_changes_none_still_carried_in_payload(self):
        """Payload should always carry the key — even when None — so the
        dispatcher can distinguish 'explicitly unknown' from 'payload was
        written by an older enqueuer that didn't carry it at all'."""
        from terrapod.services.run_service import _enqueue_vcs_status

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.has_changes = None
        run.is_drift_detection = False

        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as mock_enq:
            await _enqueue_vcs_status(run, "planning")

        _, payload = mock_enq.await_args.args
        assert "has_changes" in payload
        assert payload["has_changes"] is None

    @pytest.mark.asyncio
    async def test_drift_runs_do_not_enqueue(self):
        from terrapod.services.run_service import _enqueue_vcs_status

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.has_changes = True
        run.is_drift_detection = True

        with patch("terrapod.services.scheduler.enqueue_trigger", new=AsyncMock()) as mock_enq:
            await _enqueue_vcs_status(run, "planned")

        mock_enq.assert_not_awaited()
