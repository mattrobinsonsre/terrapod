"""Tests for VCS commit-status resolution — has-changes descriptions."""

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
