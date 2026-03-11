"""Tests for VCS poller — subdirectory filtering logic."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_workspace(**overrides):
    ws = MagicMock()
    ws.id = overrides.get("id", uuid.uuid4())
    ws.name = overrides.get("name", "test-ws")
    ws.vcs_connection_id = overrides.get("vcs_connection_id", uuid.uuid4())
    ws.vcs_repo_url = overrides.get("vcs_repo_url", "https://github.com/org/repo")
    ws.vcs_branch = overrides.get("vcs_branch", "main")
    ws.vcs_working_directory = overrides.get("vcs_working_directory", "")
    ws.vcs_last_commit_sha = overrides.get("vcs_last_commit_sha", "aaa111")
    ws.locked = False
    ws.auto_apply = False
    ws.execution_mode = "remote"
    ws.terraform_version = "1.9"
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.agent_pool_id = None
    ws.owner_email = "test@example.com"
    return ws


def _mock_connection(**overrides):
    conn = MagicMock()
    conn.provider = overrides.get("provider", "github")
    conn.status = "active"
    conn.token = "fake-token"
    conn.server_url = ""
    conn.github_app_id = 123
    conn.github_installation_id = 456
    return conn


class TestChangesAffectDirectory:
    """Unit tests for _changes_affect_directory helper."""

    def test_file_in_subdirectory_matches(self):
        from terrapod.services.vcs_poller import _changes_affect_directory

        assert _changes_affect_directory(["infra/main.tf", "README.md"], "infra") is True

    def test_file_not_in_subdirectory(self):
        from terrapod.services.vcs_poller import _changes_affect_directory

        assert _changes_affect_directory(["app/main.py", "README.md"], "infra") is False

    def test_root_file_does_not_match(self):
        from terrapod.services.vcs_poller import _changes_affect_directory

        assert _changes_affect_directory(["main.tf"], "infra") is False

    def test_nested_subdirectory_matches(self):
        from terrapod.services.vcs_poller import _changes_affect_directory

        assert _changes_affect_directory(["infra/prod/main.tf"], "infra") is True

    def test_prefix_collision_does_not_match(self):
        """'infra-old/main.tf' should NOT match working_directory='infra'."""
        from terrapod.services.vcs_poller import _changes_affect_directory

        assert _changes_affect_directory(["infra-old/main.tf"], "infra") is False

    def test_trailing_slash_stripped(self):
        from terrapod.services.vcs_poller import _changes_affect_directory

        assert _changes_affect_directory(["infra/main.tf"], "infra/") is True

    def test_empty_changed_files(self):
        from terrapod.services.vcs_poller import _changes_affect_directory

        assert _changes_affect_directory([], "infra") is False


class TestPollWorkspaceBranchFiltering:
    """Integration tests for subdirectory filtering in _poll_workspace_branch."""

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_skips_run_when_no_changes_in_directory(
        self, mock_sha, mock_changed, mock_create
    ):
        """When changes are outside working_directory, skip run but update SHA."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            vcs_working_directory="terraform/prod",
            vcs_last_commit_sha="aaa111",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = ["app/main.py", "docs/README.md"]

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_not_called()
        assert ws.vcs_last_commit_sha == "bbb222"
        mock_db.commit.assert_called_once()

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_creates_run_when_changes_in_directory(self, mock_sha, mock_changed, mock_create):
        """When changes are in working_directory, create a run."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            vcs_working_directory="terraform/prod",
            vcs_last_commit_sha="aaa111",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = ["terraform/prod/main.tf", "docs/README.md"]
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_create.return_value = mock_run

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_called_once()

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_no_filtering_without_working_directory(
        self, mock_sha, mock_changed, mock_create
    ):
        """When no working_directory set, always create run (no file check)."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(vcs_working_directory="", vcs_last_commit_sha="aaa111")
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_create.return_value = mock_run

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_changed.assert_not_called()
        mock_create.assert_called_once()

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_no_filtering_on_first_poll(self, mock_sha, mock_changed, mock_create):
        """First poll (no previous SHA) always creates run."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            vcs_working_directory="infra",
            vcs_last_commit_sha="",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_create.return_value = mock_run

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_changed.assert_not_called()
        mock_create.assert_called_once()

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_falls_through_on_api_error(self, mock_sha, mock_changed, mock_create):
        """If get_changed_files fails, create the run anyway."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            vcs_working_directory="infra",
            vcs_last_commit_sha="aaa111",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_changed.side_effect = Exception("API error")
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_create.return_value = mock_run

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_called_once()
