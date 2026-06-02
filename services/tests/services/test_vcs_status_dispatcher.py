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


class TestSupersededRunComment:
    """The shared per-PR comment must only be written by the latest run for
    that (workspace, PR) tuple. Otherwise a stale run's transition (typically
    the supersede-cancel that fires when a force-push creates a fresh run)
    can clobber the fresh run's comment with old status."""

    @staticmethod
    def _build_session(latest_run_id):
        """Mock async DB session for handle_vcs_commit_status.

        `latest_run_id` is whatever the "latest run for this (ws, PR)" query
        should return.
        """
        session = MagicMock()
        session.get = AsyncMock()
        session.execute = AsyncMock()

        # session.execute() result for the latest-run query — .scalar_one_or_none()
        latest_result = MagicMock()
        latest_result.scalar_one_or_none = MagicMock(return_value=latest_run_id)
        session.execute.return_value = latest_result
        return session

    @pytest.mark.asyncio
    async def test_superseded_run_skips_comment_but_posts_status(self):
        """Old run (not the latest for this PR) → commit status fires, comment is skipped."""
        from terrapod.services.vcs_status_dispatcher import handle_vcs_commit_status

        old_run_id = uuid.uuid4()
        new_run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        conn_id = uuid.uuid4()

        old_run = MagicMock()
        old_run.id = old_run_id
        old_run.workspace_id = ws_id
        old_run.vcs_commit_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        old_run.vcs_pull_request_number = 26
        old_run.plan_only = True
        old_run.has_changes = None

        ws = MagicMock()
        ws.id = ws_id
        ws.name = "terrapod-config"
        ws.vcs_connection_id = conn_id
        ws.vcs_repo_url = "https://github.com/markupai/terrapod-config"

        conn = MagicMock()
        conn.provider = "github"
        conn.status = "active"

        session = self._build_session(latest_run_id=new_run_id)

        async def _get(model, _id):
            from terrapod.db.models import Run, VCSConnection, Workspace

            if model is Run:
                return old_run
            if model is Workspace:
                return ws
            if model is VCSConnection:
                return conn
            return None

        session.get.side_effect = _get

        class _Ctx:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, *a):
                return False

        with (
            patch(
                "terrapod.services.vcs_status_dispatcher.get_db_session",
                lambda: _Ctx(),
            ),
            patch(
                "terrapod.services.vcs_status_dispatcher.github_service.parse_repo_url",
                return_value=("markupai", "terrapod-config"),
            ),
            patch(
                "terrapod.services.vcs_status_dispatcher.github_service.create_commit_status",
                new=AsyncMock(),
            ) as mock_status,
            patch(
                "terrapod.services.vcs_status_dispatcher.github_service.create_pr_comment",
                new=AsyncMock(),
            ) as mock_create_comment,
            patch(
                "terrapod.services.vcs_status_dispatcher.github_service.update_pr_comment",
                new=AsyncMock(),
            ) as mock_update_comment,
        ):
            await handle_vcs_commit_status(
                {
                    "run_id": str(old_run_id),
                    "workspace_id": str(ws_id),
                    "target_status": "canceled",
                    "has_changes": None,
                }
            )

        # Per-SHA commit status must still fire — GitHub only surfaces the
        # head SHA's checks, so a stale-SHA status is harmless and useful.
        mock_status.assert_awaited_once()
        # …but the shared PR comment must NOT be touched for a superseded run.
        mock_create_comment.assert_not_awaited()
        mock_update_comment.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_latest_run_writes_comment_normally(self):
        """Sanity: when the dispatched run IS the latest for the PR, the
        comment write proceeds (the new guard only short-circuits stale runs)."""
        from terrapod.services.vcs_status_dispatcher import handle_vcs_commit_status

        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        conn_id = uuid.uuid4()

        run = MagicMock()
        run.id = run_id
        run.workspace_id = ws_id
        run.vcs_commit_sha = "cafebabecafebabecafebabecafebabecafebabe"
        run.vcs_pull_request_number = 26
        run.plan_only = True
        run.has_changes = True

        ws = MagicMock()
        ws.id = ws_id
        ws.name = "terrapod-config"
        ws.vcs_connection_id = conn_id
        ws.vcs_repo_url = "https://github.com/markupai/terrapod-config"

        conn = MagicMock()
        conn.provider = "github"
        conn.status = "active"

        session = self._build_session(latest_run_id=run_id)  # itself is latest

        # The handler also runs the PlanSummary lookup via session.execute().
        # Reuse a side_effect so the FIRST execute() returns the latest-run
        # result and the SECOND returns "no ready summary".
        latest_result = MagicMock()
        latest_result.scalar_one_or_none = MagicMock(return_value=run_id)
        summary_result = MagicMock()
        summary_result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute.side_effect = [latest_result, summary_result]

        async def _get(model, _id):
            from terrapod.db.models import Run, VCSConnection, Workspace

            if model is Run:
                return run
            if model is Workspace:
                return ws
            if model is VCSConnection:
                return conn
            return None

        session.get.side_effect = _get

        class _Ctx:
            async def __aenter__(self_inner):
                return session

            async def __aexit__(self_inner, *a):
                return False

        # _find_or_create_comment uses Redis; stub it out wholesale rather
        # than mock-Redis here — the unit under test is the
        # superseded-skip guard, not the comment-cache plumbing.
        with (
            patch(
                "terrapod.services.vcs_status_dispatcher.get_db_session",
                lambda: _Ctx(),
            ),
            patch(
                "terrapod.services.vcs_status_dispatcher.github_service.parse_repo_url",
                return_value=("markupai", "terrapod-config"),
            ),
            patch(
                "terrapod.services.vcs_status_dispatcher.github_service.create_commit_status",
                new=AsyncMock(),
            ),
            patch(
                "terrapod.services.vcs_status_dispatcher._find_or_create_comment",
                new=AsyncMock(),
            ) as mock_comment,
        ):
            await handle_vcs_commit_status(
                {
                    "run_id": str(run_id),
                    "workspace_id": str(ws_id),
                    "target_status": "planned",
                    "has_changes": True,
                }
            )

        mock_comment.assert_awaited_once()
