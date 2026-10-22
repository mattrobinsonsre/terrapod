"""A push decides WHICH operations it implies, not just whether to run (#1974).

Three roots and two operations: a change under `working_directory` is a
plan/apply, a change under `config_directory` is a configure, and both can fire
from one push. Before this the poller's only question was "does this workspace
take runs at all", and a configure-only workspace answered yes -- so every push
resolved the commit, fetched the archive, wrote a configuration version and
uploaded the tarball, then threw the run away.

The decision is a pure function so the table below can be exhaustive: the thing
that goes wrong here is a combination nobody enumerated, not a database
behaviour.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.vcs_poller import (
    configure_trigger_scope,
    needs_change_list,
    operations_implied_by_push,
    plan_apply_trigger_scope,
)


def _ws(**over):
    ws = MagicMock()
    ws.id = over.get("id", uuid.uuid4())
    ws.name = "push-trigger-ws"
    ws.plan_apply_enabled = over.get("plan_apply_enabled", True)
    ws.configure_enabled = over.get("configure_enabled", False)
    ws.auto_configure_enabled = over.get("auto_configure_enabled", False)
    ws.configure_after_apply = over.get("configure_after_apply", False)
    ws.working_directory = over.get("working_directory", "terraform")
    ws.trigger_prefixes = over.get("trigger_prefixes", [])
    ws.config_directory = over.get("config_directory", "ansible")
    ws.inventory_paths = over.get("inventory_paths", [])
    ws.default_playbook = over.get("default_playbook", "site.yml")
    ws.default_limit = over.get("default_limit", "")
    return ws


_TF = ["terraform/main.tf"]
_ANSIBLE = ["ansible/roles/base/tasks/main.yml"]
_BOTH = _TF + _ANSIBLE
_NEITHER = ["README.md"]


class TestWhichOperationsAPushImplies:
    @pytest.mark.parametrize(
        ("changed", "expected"),
        [
            (_TF, (True, False)),
            (_ANSIBLE, (False, True)),
            (_BOTH, (True, True)),
            (_NEITHER, (False, False)),
            # The provider truncated the comparison, so we cannot tell what
            # changed and must not skip -- the long-standing reading, extended
            # to the second operation.
            (None, (True, True)),
        ],
    )
    def test_the_scopes_are_independent(self, changed, expected):
        ws = _ws(configure_enabled=True, auto_configure_enabled=True)
        assert operations_implied_by_push(ws, changed) == expected

    def test_a_configure_only_workspace_ignores_a_terraform_change(self):
        """And this is the case that was doing per-push work for nothing: it
        used to reach `create_run`, be refused, and discard a configuration
        version it had just uploaded."""
        ws = _ws(
            plan_apply_enabled=False,
            configure_enabled=True,
            auto_configure_enabled=True,
            working_directory="",
        )
        assert operations_implied_by_push(ws, _TF) == (False, False)
        assert operations_implied_by_push(ws, _ANSIBLE) == (False, True)

    def test_the_trigger_is_opt_in(self):
        """Declaring the operation is not asking for the trigger. A workspace
        with a configure and no `auto_configure_enabled` is driven manually,
        which is the default."""
        ws = _ws(configure_enabled=True, auto_configure_enabled=False)
        assert operations_implied_by_push(ws, _ANSIBLE) == (False, False)

    def test_the_operation_must_be_declared(self):
        """A trigger set on a workspace with no configure operation is inert
        rather than refused at the write boundary, so the poller is where that
        has to be read -- otherwise the push would queue a run `create_run`
        then refuses."""
        ws = _ws(configure_enabled=False, auto_configure_enabled=True)
        assert operations_implied_by_push(ws, _ANSIBLE) == (False, False)

    def test_a_composite_workspace_gets_one_run_not_two(self):
        """The plan/apply run already carries the configure, so firing a
        standalone one as well would run the playbook twice for one push --
        once before the apply had written the inventory it reads."""
        ws = _ws(
            configure_enabled=True,
            auto_configure_enabled=True,
            configure_after_apply=True,
        )
        assert operations_implied_by_push(ws, _BOTH) == (True, False)

    def test_a_composite_workspace_still_configures_on_an_ansible_only_push(self):
        """Nothing to compose with: the push implies no plan/apply, so the
        configure is standalone. Suppressing it would mean a workspace with
        `configure_after_apply` on could never configure from a playbook
        change, which is the opposite of what both settings ask for."""
        ws = _ws(
            configure_enabled=True,
            auto_configure_enabled=True,
            configure_after_apply=True,
        )
        assert operations_implied_by_push(ws, _ANSIBLE) == (False, True)


class TestAnEmptyScopeIsTheWholeRepository:
    def test_no_terraform_narrowing(self):
        ws = _ws(working_directory="", trigger_prefixes=[], configure_enabled=False)
        assert plan_apply_trigger_scope(ws) == []
        assert operations_implied_by_push(ws, _NEITHER) == (True, False)

    def test_trigger_prefixes_win_over_the_working_directory(self):
        ws = _ws(working_directory="terraform", trigger_prefixes=["modules"])
        assert plan_apply_trigger_scope(ws) == ["modules"]
        assert operations_implied_by_push(ws, _TF) == (False, False)
        assert operations_implied_by_push(ws, ["modules/vpc/main.tf"]) == (True, False)

    def test_an_ansible_root_at_the_repository_root(self):
        """`config_directory == ""` means the Ansible root IS the top of the
        tree, so every push is in scope -- the same asymmetry the fetch scope
        documents, and the reason this cannot go through the strict prefix
        matcher, which never matches an empty prefix."""
        ws = _ws(
            config_directory="",
            configure_enabled=True,
            auto_configure_enabled=True,
            working_directory="terraform",
        )
        assert configure_trigger_scope(ws) == []
        assert operations_implied_by_push(ws, _NEITHER) == (False, True)


class TestTheChangedFileListIsOnlyFetchedWhenItCanNarrow:
    """An extra provider call per push per workspace is not free -- VCS rate
    limits are a real constraint -- and a workspace whose every scope is the
    whole repository cannot be narrowed by the answer."""

    def test_no_narrowing_at_all(self):
        ws = _ws(working_directory="", trigger_prefixes=[], configure_enabled=False)
        assert needs_change_list(ws) is False

    def test_a_terraform_narrowing_needs_it(self):
        assert needs_change_list(_ws(working_directory="terraform")) is True

    def test_an_ansible_narrowing_alone_needs_it(self):
        """The half that is new. A workspace narrowed only by its Ansible root
        still needs the list, because that root is what decides whether the
        configure fires -- and the pre-existing check asked about the
        plan/apply scope alone."""
        ws = _ws(
            working_directory="",
            trigger_prefixes=[],
            configure_enabled=True,
            auto_configure_enabled=True,
            config_directory="ansible",
        )
        assert needs_change_list(ws) is True

    def test_an_ansible_root_at_the_top_needs_nothing(self):
        ws = _ws(
            working_directory="",
            trigger_prefixes=[],
            configure_enabled=True,
            auto_configure_enabled=True,
            config_directory="",
        )
        assert needs_change_list(ws) is False


class TestThePollerCreatesWhatThePushImplies:
    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_a_push_to_both_roots_creates_both_runs(
        self, mock_sha, mock_changed, mock_create
    ):
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _ws(
            configure_enabled=True,
            auto_configure_enabled=True,
            vcs_last_commit_sha="aaa111",
        )
        ws.vcs_last_commit_sha = "aaa111"
        ws.autodiscovery_rule_id = None
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = _BOTH
        plan_run = MagicMock(id=uuid.uuid4())
        plan_run.configuration_version_id = uuid.uuid4()
        configure_run = MagicMock(id=uuid.uuid4())
        mock_create.side_effect = [plan_run, configure_run]

        await _poll_workspace_branch(AsyncMock(), ws, _conn(), "org", "repo", "main")

        assert mock_create.await_count == 2
        first, second = mock_create.await_args_list
        assert first.kwargs.get("is_configure", False) is False
        assert second.kwargs["is_configure"] is True
        # The configure reuses the configuration version the plan/apply run
        # just built: both must execute the same commit's code, and a second
        # one would upload the identical tarball to say so.
        assert second.kwargs["reuse_cv_id"] == plan_run.configuration_version_id

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_an_ansible_only_push_creates_only_a_configure(
        self, mock_sha, mock_changed, mock_create
    ):
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _ws(configure_enabled=True, auto_configure_enabled=True)
        ws.vcs_last_commit_sha = "aaa111"
        ws.autodiscovery_rule_id = None
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = _ANSIBLE
        mock_create.return_value = MagicMock(id=uuid.uuid4())

        await _poll_workspace_branch(AsyncMock(), ws, _conn(), "org", "repo", "main")

        assert mock_create.await_count == 1
        assert mock_create.await_args.kwargs["is_configure"] is True
        # No configuration version to reuse: this run built its own.
        assert mock_create.await_args.kwargs["reuse_cv_id"] is None

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_a_configure_only_workspace_with_no_trigger_fetches_nothing(
        self, mock_sha, mock_changed, mock_create
    ):
        """The per-push waste, closed. The run is not merely skipped at the end
        -- nothing is fetched at all, because the decision is made before the
        archive."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _ws(
            plan_apply_enabled=False,
            configure_enabled=True,
            auto_configure_enabled=False,
            working_directory="",
        )
        ws.vcs_last_commit_sha = "aaa111"
        ws.autodiscovery_rule_id = None
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = _ANSIBLE

        await _poll_workspace_branch(AsyncMock(), ws, _conn(), "org", "repo", "main")

        mock_create.assert_not_called()
        # And the cursor still advanced: the commit has been dealt with, so the
        # next cycle must not re-examine it.
        assert ws.vcs_last_commit_sha == "bbb222"


def _conn():
    conn = MagicMock()
    conn.provider = "github"
    conn.id = uuid.uuid4()
    return conn
