"""Tests for VCS poller — subdirectory filtering and VCS error tracking."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_workspace(**overrides):
    ws = MagicMock()
    ws.id = overrides.get("id", uuid.uuid4())
    ws.name = overrides.get("name", "test-ws")
    ws.vcs_connection_id = overrides.get("vcs_connection_id", uuid.uuid4())
    ws.vcs_repo_url = overrides.get("vcs_repo_url", "https://github.com/org/repo")
    ws.vcs_branch = overrides.get("vcs_branch", "main")
    ws.working_directory = overrides.get("working_directory", "")
    ws.trigger_prefixes = overrides.get("trigger_prefixes", [])
    ws.vcs_last_commit_sha = overrides.get("vcs_last_commit_sha", "aaa111")
    ws.vcs_last_polled_at = overrides.get("vcs_last_polled_at", None)
    ws.vcs_last_error = overrides.get("vcs_last_error", None)
    ws.vcs_last_error_at = overrides.get("vcs_last_error_at", None)
    ws.locked = False
    ws.auto_apply = False
    ws.execution_mode = "agent"
    ws.terraform_version = "1.11"
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


class TestChangesAffectPrefixes:
    """Unit tests for _changes_affect_prefixes helper."""

    def test_single_prefix_matches(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["infra/main.tf", "README.md"], ["infra"]) is True

    def test_multiple_prefixes_any_match(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["modules/vpc/main.tf"], ["infra", "modules"]) is True

    def test_no_prefix_matches(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["app/main.py", "README.md"], ["infra"]) is False

    def test_empty_prefix_list(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["infra/main.tf"], []) is False

    def test_prefix_collision(self):
        """'infra-old/main.tf' should NOT match prefix 'infra'."""
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["infra-old/main.tf"], ["infra"]) is False

    def test_trailing_slash_stripped(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["infra/main.tf"], ["infra/"]) is True

    def test_empty_changed_files(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes([], ["infra"]) is False

    def test_nested_subdirectory_matches(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["infra/prod/main.tf"], ["infra"]) is True

    def test_root_file_does_not_match(self):
        from terrapod.services.vcs_poller import _changes_affect_prefixes

        assert _changes_affect_prefixes(["main.tf"], ["infra"]) is False


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
            working_directory="terraform/prod",
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
            working_directory="terraform/prod",
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

        ws = _mock_workspace(working_directory="", vcs_last_commit_sha="aaa111")
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
            working_directory="infra",
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
            working_directory="infra",
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

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_creates_run_when_truncated(self, mock_sha, mock_changed, mock_create):
        """When get_changed_files returns None (truncated), create run anyway."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            working_directory="infra",
            vcs_last_commit_sha="aaa111",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = None  # truncated response
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_create.return_value = mock_run

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_called_once()

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_uses_trigger_prefixes_over_working_dir(
        self, mock_sha, mock_changed, mock_create
    ):
        """When trigger_prefixes is set, it overrides working_directory for filtering."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            working_directory="infra",
            trigger_prefixes=["modules"],
            vcs_last_commit_sha="aaa111",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = ["modules/vpc/main.tf"]
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_create.return_value = mock_run

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        # Change is in modules/ which matches trigger_prefixes, even though
        # it's outside working_directory ("infra")
        mock_create.assert_called_once()

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_trigger_prefix_matches_outside_working_dir(
        self, mock_sha, mock_changed, mock_create
    ):
        """Trigger prefixes can match directories outside the working directory."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            working_directory="environments/dev",
            trigger_prefixes=["environments/dev", "modules"],
            vcs_last_commit_sha="aaa111",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = ["modules/vpc/main.tf"]
        mock_run = MagicMock()
        mock_run.id = uuid.uuid4()
        mock_create.return_value = mock_run

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_called_once()

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_changed_files")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_trigger_prefixes_skip_when_no_match(self, mock_sha, mock_changed, mock_create):
        """When trigger_prefixes is set but no files match, skip the run."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(
            working_directory="environments/dev",
            trigger_prefixes=["environments/dev", "modules"],
            vcs_last_commit_sha="aaa111",
        )
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_changed.return_value = ["environments/staging/main.tf", "README.md"]

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_not_called()
        assert ws.vcs_last_commit_sha == "bbb222"
        mock_db.commit.assert_called_once()


class TestPollWorkspaceVCSErrorTracking:
    """Tests for VCS error state tracking in _poll_workspace."""

    @patch("terrapod.services.vcs_poller._poll_workspace_prs")
    @patch("terrapod.services.vcs_poller._poll_workspace_branch")
    @patch("terrapod.services.vcs_poller._resolve_branch")
    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_sets_last_polled_on_success(
        self, mock_parse, mock_resolve, mock_branch, mock_prs
    ):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace()
        conn = _mock_connection()
        mock_parse.return_value = ("org", "repo")
        mock_resolve.return_value = "main"
        mock_branch.return_value = None
        mock_prs.return_value = None

        mock_db = AsyncMock()
        mock_db.get.return_value = conn

        await _poll_workspace(mock_db, ws)

        assert ws.vcs_last_polled_at is not None
        assert ws.vcs_last_error is None
        assert ws.vcs_last_error_at is None

    @patch("terrapod.services.vcs_poller._poll_workspace_prs")
    @patch("terrapod.services.vcs_poller._poll_workspace_branch")
    @patch("terrapod.services.vcs_poller._resolve_branch")
    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_sets_error_on_failure(self, mock_parse, mock_resolve, mock_branch, mock_prs):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace()
        conn = _mock_connection()
        mock_parse.return_value = ("org", "repo")
        mock_resolve.return_value = "main"
        mock_branch.side_effect = Exception("403 Forbidden")

        mock_db = AsyncMock()
        mock_db.get.return_value = conn

        await _poll_workspace(mock_db, ws)

        assert ws.vcs_last_error == "403 Forbidden"
        assert ws.vcs_last_error_at is not None

    @patch("terrapod.services.vcs_poller._poll_workspace_prs")
    @patch("terrapod.services.vcs_poller._poll_workspace_branch")
    @patch("terrapod.services.vcs_poller._resolve_branch")
    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_clears_error_on_recovery(self, mock_parse, mock_resolve, mock_branch, mock_prs):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace(
            vcs_last_error="previous error",
            vcs_last_error_at=datetime.now(UTC),
        )
        conn = _mock_connection()
        mock_parse.return_value = ("org", "repo")
        mock_resolve.return_value = "main"
        mock_branch.return_value = None
        mock_prs.return_value = None

        mock_db = AsyncMock()
        mock_db.get.return_value = conn

        await _poll_workspace(mock_db, ws)

        assert ws.vcs_last_polled_at is not None
        assert ws.vcs_last_error is None
        assert ws.vcs_last_error_at is None

    async def test_inactive_connection_sets_error(self):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace()
        conn = _mock_connection()
        conn.status = "inactive"

        mock_db = AsyncMock()
        mock_db.get.return_value = conn

        await _poll_workspace(mock_db, ws)

        assert ws.vcs_last_error == "VCS connection is not active"
        assert ws.vcs_last_error_at is not None

    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_unparseable_url_sets_error(self, mock_parse):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace(vcs_repo_url="not-a-valid-url")
        conn = _mock_connection()
        mock_parse.return_value = None

        mock_db = AsyncMock()
        mock_db.get.return_value = conn

        await _poll_workspace(mock_db, ws)

        assert "Cannot parse VCS repo URL" in ws.vcs_last_error
        assert ws.vcs_last_error_at is not None

    @patch("terrapod.services.vcs_poller._resolve_branch")
    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_unresolvable_branch_sets_error(self, mock_parse, mock_resolve):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace()
        conn = _mock_connection()
        mock_parse.return_value = ("org", "repo")
        mock_resolve.return_value = None

        mock_db = AsyncMock()
        mock_db.get.return_value = conn

        await _poll_workspace(mock_db, ws)

        assert ws.vcs_last_error == "Cannot determine tracked branch"
        assert ws.vcs_last_error_at is not None


class TestPollWorkspaceBranchRaceCondition:
    """Tests for the CAS + dedup protection against concurrent VCS polls (issue #217)."""

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_cas_success_proceeds_to_create_run(self, mock_sha, mock_create):
        """When the CAS affects a row (no concurrent poll won), we create a run."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(vcs_last_commit_sha="aaa111", working_directory="")
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"
        mock_create.return_value = MagicMock(id=uuid.uuid4())

        # Simulate CAS affecting 1 row: scalar_one_or_none returns a non-None id
        cas_result = MagicMock()
        cas_result.scalar_one_or_none = MagicMock(return_value=ws.id)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=cas_result)

        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_called_once()
        assert ws.vcs_last_commit_sha == "bbb222"

    @patch("terrapod.services.vcs_poller._create_vcs_run")
    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_cas_miss_bails_without_creating_run(self, mock_sha, mock_create):
        """When the CAS affects zero rows (another poll won the race), bail silently."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(vcs_last_commit_sha="aaa111", working_directory="")
        conn = _mock_connection()
        mock_sha.return_value = "bbb222"

        # Simulate CAS affecting 0 rows: scalar_one_or_none returns None
        cas_result = MagicMock()
        cas_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=cas_result)

        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        mock_create.assert_not_called()
        # The losing poll must not mutate the in-memory ws state
        assert ws.vcs_last_commit_sha == "aaa111"

    @patch("terrapod.services.vcs_poller._get_branch_sha")
    async def test_sha_unchanged_early_returns(self, mock_sha):
        """If the branch SHA still matches vcs_last_commit_sha, no CAS attempted."""
        from terrapod.services.vcs_poller import _poll_workspace_branch

        ws = _mock_workspace(vcs_last_commit_sha="aaa111")
        conn = _mock_connection()
        mock_sha.return_value = "aaa111"

        mock_db = AsyncMock()
        await _poll_workspace_branch(mock_db, ws, conn, "org", "repo", "main")

        # No DB writes at all — early return before CAS
        mock_db.execute.assert_not_called()
        mock_db.commit.assert_not_called()


class TestCreateVcsRunDedup:
    """Defensive dedup in _create_vcs_run prevents duplicate runs for the same commit."""

    async def test_returns_none_when_duplicate_exists(self):
        """If a run already exists for (workspace, sha, branch, pr_number), skip."""
        from terrapod.services.vcs_poller import _create_vcs_run

        ws = _mock_workspace()
        conn = _mock_connection()

        existing_run = MagicMock()
        existing_run.id = uuid.uuid4()

        # First mock_db.execute call is the dedup SELECT — return existing run
        dedup_result = MagicMock()
        dedup_result.scalar_one_or_none = MagicMock(return_value=existing_run)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=dedup_result)

        with patch("terrapod.services.vcs_poller._download_archive") as mock_dl:
            run = await _create_vcs_run(
                mock_db, ws, conn, "org", "repo", "bbb222", "main", message="test"
            )

        assert run is None
        # We must bail before wasting bandwidth on the archive download
        mock_dl.assert_not_called()

    async def test_dedup_distinguishes_pr_number(self):
        """A PR-scoped run must not dedup against a branch-push run with the same SHA."""
        from terrapod.services.vcs_poller import _create_vcs_run

        ws = _mock_workspace()
        conn = _mock_connection()

        # Dedup query returns nothing (no matching run exists)
        dedup_result = MagicMock()
        dedup_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=dedup_result)

        # Simulate the archive download failing so we don't need to mock the whole
        # create-run chain — the assertion is that dedup passed (download was tried).
        with patch(
            "terrapod.services.vcs_poller._download_archive",
            side_effect=RuntimeError("stop here"),
        ) as mock_dl:
            run = await _create_vcs_run(
                mock_db,
                ws,
                conn,
                "org",
                "repo",
                "bbb222",
                "main",
                pr_number=42,
                message="test",
            )

        assert run is None  # Download failed, returns None
        mock_dl.assert_called_once()  # But dedup let us through — the key assertion
