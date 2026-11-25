"""The apply gate can tell a block Terrapod inflicted on itself from a real one (#2707).

Two halves, and the second is the one that matters.

The predicate is pure and easy to pin. The **call site** is where the earlier
instances of this class went wrong: an optional relaxation makes the bypass the
default, so every pre-existing test takes the old branch and a green suite says
nothing about the new one. `_check_mergeability_or_block` had no behavioural
test of any kind before this file, so the mutation that must fail is "the route
stops consulting the predicate", not "the predicate returns the wrong answer".
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import github_service, run_service, vcs_mergeability
from terrapod.services.vcs_provider import MergeabilityStatus


@pytest.fixture
def declared():
    """The operator has declared the aggregate apply status a required check."""
    before = settings.vcs.commit_statuses.apply_is_required_check
    settings.vcs.commit_statuses.apply_is_required_check = True
    try:
        yield
    finally:
        settings.vcs.commit_statuses.apply_is_required_check = before


# ── the predicate ────────────────────────────────────────────────────────


class TestThePredicate:
    def test_the_default_disregards_nothing(self):
        """Off unless declared: the deployment default must be today's behaviour."""
        assert settings.vcs.commit_statuses.apply_is_required_check is False
        assert vcs_mergeability.block_is_self_inflicted("github", "blocked") is False

    def test_a_declared_required_check_makes_github_blocked_self_inflicted(self, declared):
        assert vcs_mergeability.block_is_self_inflicted("github", "blocked") is True

    @pytest.mark.parametrize("state", ["dirty", "behind", "draft", "closed", "unknown"])
    def test_every_other_github_state_still_refuses(self, declared, state):
        """`blocked` is the only state a pending required status of ours produces.

        Merge conflicts and an out-of-date branch are real reasons not to apply,
        and relaxing them would be the hole this is often mistaken for.
        """
        assert vcs_mergeability.block_is_self_inflicted("github", state) is False

    @pytest.mark.parametrize("state", ["blocked", "dirty", "ci_must_pass", "not_approved"])
    def test_gitlab_disregards_nothing(self, declared, state):
        """Documentation-derived; see the module docstring.

        GitLab has no per-name required check -- a commit status becomes a job
        on the merge request's pipeline -- so there is nothing to exempt, and
        `_GITLAB_MERGEABLE_DETAILED` already tolerates a pipeline that has not
        finished. The switch must therefore do nothing there rather than
        quietly taking GitHub's answer.
        """
        assert vcs_mergeability.block_is_self_inflicted("gitlab", state) is False

    def test_an_unknown_provider_fails_closed(self, declared):
        assert vcs_mergeability.block_is_self_inflicted("bitbucket", "blocked") is False

    def test_turning_the_aggregates_off_cannot_leave_the_gate_relaxed(self, declared):
        """With `enabled: false` nothing posts the status, so no block is ours."""
        before = settings.vcs.commit_statuses.enabled
        settings.vcs.commit_statuses.enabled = False
        try:
            assert vcs_mergeability.apply_is_required_check() is False
            assert vcs_mergeability.block_is_self_inflicted("github", "blocked") is False
        finally:
            settings.vcs.commit_statuses.enabled = before

    def test_every_disregarded_state_is_one_the_gate_would_otherwise_refuse(self):
        """Derived rather than listed, so a new entry cannot be dead on arrival.

        Disregarding a state the mergeability check never refuses on would be an
        entry that reads as a relaxation and relaxes nothing -- and the reverse,
        a typo, would silently stop the switch working at all.
        """
        assert vcs_mergeability.SELF_INFLICTED_STATES["github"] <= (
            github_service._GITHUB_BLOCKING_STATES
        )
        assert vcs_mergeability.SELF_INFLICTED_STATES["github"], (
            "an empty set makes the switch inert while the config key still advertises it"
        )

    def test_every_provider_the_gate_dispatches_to_has_an_entry(self):
        """The dispatch in `_check_mergeability_or_block` names exactly these two."""
        assert set(vcs_mergeability.SELF_INFLICTED_STATES) == {"github", "gitlab"}


# ── the call site ────────────────────────────────────────────────────────


def _run():
    run = MagicMock()
    run.id = "run-1"
    run.vcs_pull_request_number = 7
    run.vcs_apply_blocked_reason = "stale reason from a previous attempt"
    run.workspace_id = "ws-1"
    return run


def _db(provider: str = "github"):
    workspace = MagicMock()
    workspace.vcs_workflow = "apply_then_merge"
    workspace.vcs_connection_id = "vcs-1"
    workspace.vcs_repo_url = "https://github.com/default/repo"
    conn = MagicMock()
    conn.provider = provider

    db = MagicMock()
    db.get = AsyncMock(side_effect=[workspace, conn])
    return db


def _status(state: str, *, unknown: bool = False) -> MergeabilityStatus:
    return MergeabilityStatus(
        mergeable=False, state=state, reason=f"GitHub reports {state}", unknown=unknown
    )


class TestTheRouteConsultsIt:
    async def test_blocked_still_refuses_when_nothing_was_declared(self):
        run = _run()
        with patch.object(
            github_service,
            "get_pull_request_mergeability",
            new=AsyncMock(return_value=_status("blocked")),
        ):
            with pytest.raises(run_service.ApplyBlocked):
                await run_service._check_mergeability_or_block(_db(), run)

    async def test_blocked_is_allowed_through_once_declared(self, declared):
        """The mutation this file exists for: drop the predicate from the route
        and this is the only test that fails."""
        run = _run()
        with patch.object(
            github_service,
            "get_pull_request_mergeability",
            new=AsyncMock(return_value=_status("blocked")),
        ):
            await run_service._check_mergeability_or_block(_db(), run)
        assert run.vcs_apply_blocked_reason is None, (
            "a disregarded block must clear the reason, or the PR comment keeps "
            "telling the operator the apply is held"
        )

    @pytest.mark.parametrize("state", ["dirty", "behind"])
    async def test_a_real_block_still_refuses_once_declared(self, declared, state):
        run = _run()
        with patch.object(
            github_service,
            "get_pull_request_mergeability",
            new=AsyncMock(return_value=_status(state)),
        ):
            with pytest.raises(run_service.ApplyBlocked) as exc:
                await run_service._check_mergeability_or_block(_db(), run)
        assert state in exc.value.reason

    async def test_the_transient_state_still_refuses_once_declared(self, declared):
        """`mergeable: null` is GitHub still computing, not a block of ours.

        It is checked before the disregard, so a retry is still what the caller
        is told to do -- and the state carried alongside it can be anything.
        """
        run = _run()
        with patch.object(
            github_service,
            "get_pull_request_mergeability",
            new=AsyncMock(return_value=_status("blocked", unknown=True)),
        ):
            with pytest.raises(run_service.ApplyBlocked):
                await run_service._check_mergeability_or_block(_db(), run)

    async def test_gitlab_is_unaffected_by_the_switch(self, declared):
        """Keyed on the provider, so GitLab keeps refusing on a real block."""
        run = _run()
        with patch.object(
            run_service.gitlab_service,
            "get_pull_request_mergeability",
            new=AsyncMock(return_value=_status("blocked")),
        ):
            with pytest.raises(run_service.ApplyBlocked):
                await run_service._check_mergeability_or_block(_db("gitlab"), run)

    async def test_a_mergeable_pr_is_unchanged_either_way(self, declared):
        run = _run()
        with patch.object(
            github_service,
            "get_pull_request_mergeability",
            new=AsyncMock(
                return_value=MergeabilityStatus(mergeable=True, state="clean", reason="")
            ),
        ):
            await run_service._check_mergeability_or_block(_db(), run)
        assert run.vcs_apply_blocked_reason is None
