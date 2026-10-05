"""Tests for VCS commit-status resolution — has-changes descriptions."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.db.models import VCSConnection
from terrapod.services import vcs_status_dispatcher as dispatcher
from terrapod.services.vcs_status_dispatcher import _resolve_status


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


class TestAHeldRunSaysWhatIsHoldingIt:
    def test_each_gate_names_itself_and_the_way_out(self):
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        for gate, expected in [
            ("policy", "policy check"),
            ("security-scan", "security scan"),
            ("run-task", "run task"),
            # Added by #1766 and the reason this matters most: that gate holds
            # a run while its verdict is produced, so the wait is real.
            ("ai-policy", "AI policy gate"),
        ]:
            gh, gl, description = _resolve_status("planning", False, None, gate)
            assert expected in description, (gate, description)
            assert "Plan in progress" not in description
            # Pending, not failure: the plan succeeded and a decision is owed.
            # It still leaves a required check unmet, so the PR cannot merge.
            assert (gh, gl) == ("pending", "running")

    def test_an_unheld_planning_run_is_unchanged(self):
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        assert _resolve_status("planning", False, None, None) == (
            "pending",
            "running",
            "Plan in progress",
        )

    def test_a_gate_we_do_not_recognise_still_says_blocked(self):
        """A newer API naming a gate this build does not know must not fall
        back to "Plan in progress" — the run is stopped either way."""
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        _, _, description = _resolve_status("planning", False, None, "something-new")
        assert "Blocked" in description

    def test_the_gate_only_applies_while_planning(self):
        """A stale gate value must not rewrite a terminal status."""
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        assert _resolve_status("applied", False, None, "policy")[2] == "Apply complete"


# ── a no-op run is not an apply (#1794) ──────────────────────────────


class TestANoOpRunDoesNotClaimToHaveApplied:
    def test_a_zero_change_applied_run_says_there_was_nothing_to_apply(self):
        """The run reaches `applied` without launching an apply, deliberately.
        "Apply complete" read as though something had been applied — alarming
        on a workspace with auto-apply off, where nobody confirmed anything."""
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        gh, gl, description = _resolve_status("applied", False, has_changes=False)
        assert "nothing to apply" in description
        assert "Apply complete" not in description
        # Still a success: the run did everything it needed to.
        assert (gh, gl) == ("success", "success")

    def test_a_real_apply_is_untouched(self):
        from terrapod.services.vcs_status_dispatcher import _resolve_status

        assert _resolve_status("applied", False, has_changes=True)[2] == "Apply complete"
        # Unknown (an older run, or the flag never landed) keeps the old text
        # rather than claiming a no-op we cannot demonstrate.
        assert _resolve_status("applied", False, has_changes=None)[2] == "Apply complete"


# ── one PR, one comment — and this module does not write it (#1940) ───
#
# This module used to post its own per-workspace comment, identified by
# `<!-- terrapod:ws:{id}:{sha} -->`. The SHA in that identity meant a push
# could never match the previous comment, so it posted a new one every time,
# while the status table beside it was edited in place for ever. A
# four-workspace PR with four pushes carried seventeen Terrapod comments.
#
# The whole surface is retired. `vcs_status_comment` renders one comment per
# PR with every workspace's narrative and gates folded into it, and this
# module's only remaining job on a PR is to ask for a refresh.


class TestTheDispatcherDoesNotWriteAComment:
    """Structural: the retired surface must not creep back.

    A behavioural test cannot see a *new* comment-posting path being added
    here, which is exactly how the second surface arrived the first time. So
    this asserts the absence of the machinery itself, by name, and that the
    module makes no VCS comment call of its own.
    """

    RETIRED = (
        "_build_comment_body",
        "_comment_marker",
        "_find_or_create_comment",
        "_render_ai_summary_section",
        "_pr_has_status_table",
        "_acquire_comment_lock",
        "_release_comment_lock",
    )

    def test_no_comment_building_or_posting_surface_remains(self):
        present = [name for name in self.RETIRED if hasattr(dispatcher, name)]
        assert present == [], (
            f"{present} is back on vcs_status_dispatcher. One PR gets one comment, "
            "rendered by vcs_status_comment — see #1940."
        )

    def test_the_module_makes_no_comment_api_call(self):
        """Grep the source, not the namespace: a call added inline to the
        handler would not show up as a module attribute."""
        import inspect

        src = inspect.getsource(dispatcher)
        for call in (
            "create_pr_comment",
            "update_pr_comment",
            "create_mr_comment",
            "update_mr_comment",
            "list_pr_comments",
            "list_mr_comments",
        ):
            assert f".{call}(" not in src, (
                f"{call} is called from vcs_status_dispatcher again; the PR comment "
                "belongs to vcs_status_comment (#1940)."
            )


class TestTheDispatcherAsksForARefresh:
    """A PR run must still cause the one comment to be brought up to date.

    Deleting the old posting path without this would leave the commit status
    updating while the comment froze — the feature would look fine and report
    nothing, which is worse than the duplication it replaced.
    """

    @staticmethod
    def _session(run, ws, conn):
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        session.execute = AsyncMock(return_value=result)

        async def _get(model, _id):
            from terrapod.db.models import Run, VCSConnection, Workspace

            return {Run: run, Workspace: ws, VCSConnection: conn}.get(model)

        session.get = AsyncMock(side_effect=_get)
        return session

    def _fixtures(self, *, pr_number):
        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.vcs_commit_sha = "cafebabecafebabecafebabecafebabecafebabe"
        run.vcs_pull_request_number = pr_number
        run.plan_only = True
        run.has_changes = True
        run.status = "planned"

        ws = MagicMock()
        ws.id = run.workspace_id
        ws.name = "prod-vpc"
        ws.vcs_connection_id = uuid.uuid4()
        ws.vcs_repo_url = "https://github.com/org/repo"

        conn = MagicMock(spec=VCSConnection)
        conn.id = ws.vcs_connection_id
        conn.provider = "github"
        conn.status = "active"
        conn.server_url = "https://github.com"
        return run, ws, conn

    async def _dispatch(self, *, pr_number):
        from terrapod.services.vcs_status_dispatcher import handle_vcs_commit_status

        run, ws, conn = self._fixtures(pr_number=pr_number)
        session = self._session(run, ws, conn)

        class _Ctx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *a):
                return False

        with (
            patch("terrapod.services.vcs_status_dispatcher.get_db_session", return_value=_Ctx()),
            patch.object(
                dispatcher.github_service, "create_commit_status", new=AsyncMock()
            ) as status,
            patch.object(
                dispatcher.vcs_status_comment, "refresh_for_run", new=AsyncMock()
            ) as refresh,
        ):
            await handle_vcs_commit_status(
                {
                    "run_id": str(run.id),
                    "workspace_id": str(ws.id),
                    "target_status": "planned",
                    "has_changes": True,
                }
            )
        return status, refresh

    @pytest.mark.asyncio
    async def test_a_pr_run_refreshes_the_one_comment(self):
        status, refresh = await self._dispatch(pr_number=7)
        status.assert_awaited_once()
        refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_branch_run_posts_a_status_and_asks_for_no_refresh(self):
        """A run with no PR has no PR comment to refresh. The commit status is
        the whole of its reporting, and must still fire."""
        status, refresh = await self._dispatch(pr_number=None)
        status.assert_awaited_once()
        refresh.assert_not_awaited()
