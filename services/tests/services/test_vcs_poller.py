"""Tests for VCS poller — subdirectory filtering and VCS error tracking."""

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # noqa: F401  # used by tests appended later via @pytest.mark.asyncio


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
    ws.vcs_last_attempted_at = overrides.get("vcs_last_attempted_at", None)
    ws.vcs_last_error = overrides.get("vcs_last_error", None)
    ws.vcs_last_error_at = overrides.get("vcs_last_error_at", None)
    ws.locked = False
    ws.auto_apply = False
    ws.execution_mode = "agent"
    ws.terraform_version = "1.11"
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
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

        with patch(
            "terrapod.services.vcs_archive_cache.VCSArchiveCache.get_or_fetch",
            new_callable=AsyncMock,
        ) as mock_fetch:
            run = await _create_vcs_run(
                mock_db, ws, conn, "org", "repo", "bbb222", "main", message="test"
            )

        assert run is None
        # We must bail before wasting bandwidth on the archive download
        mock_fetch.assert_not_called()

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

        # Simulate the archive fetch failing so we don't need to mock the whole
        # create-run chain — the assertion is that dedup passed (cache was tried).
        with patch(
            "terrapod.services.vcs_archive_cache.VCSArchiveCache.get_or_fetch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop here"),
        ) as mock_fetch:
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

        assert run is None  # Fetch failed, returns None
        mock_fetch.assert_called_once()  # But dedup let us through — the key assertion


# ── poll_cycle parallelism + repo-scoped immediate polls ─────────────


class TestPollCycleParallel:
    @pytest.mark.asyncio
    async def test_poll_cycle_polls_workspaces_in_parallel(self):
        """poll_cycle must not serialise per-workspace polls — each workspace
        runs in its own session, all concurrently, bounded by a semaphore."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services.vcs_poller import poll_cycle

        ws_ids = [uuid.uuid4() for _ in range(5)]

        # get_db_session() is a context manager yielding the db.
        mock_db = AsyncMock()
        select_result = MagicMock()
        select_result.all = MagicMock(return_value=[(wid,) for wid in ws_ids])
        mock_db.execute = AsyncMock(return_value=select_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        # Track concurrent entries rather than timing — robust on slow CI.
        in_flight: list[int] = [0]
        max_in_flight: list[int] = [0]
        entered = 0

        async def fake_owned_poll(ws_id, semaphore, cache=None, meta=None, paths_unions=None):
            import asyncio as _a

            nonlocal entered
            async with semaphore:
                entered += 1
                in_flight[0] += 1
                max_in_flight[0] = max(max_in_flight[0], in_flight[0])
                # Yield so other coroutines can enter before we release.
                await _a.sleep(0.01)
                in_flight[0] -= 1

        with (
            patch("terrapod.services.vcs_poller.get_db_session", return_value=mock_ctx),
            patch(
                "terrapod.services.vcs_poller._poll_workspace_owned",
                side_effect=fake_owned_poll,
            ),
            patch(
                "terrapod.services.vcs_poller._compute_paths_unions",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await poll_cycle()

        # All 5 workspaces should execute, with at least 2 concurrently. If the
        # poller were serial, max_in_flight would be 1.
        assert entered == 5
        assert max_in_flight[0] >= 2, (
            f"workspaces never ran concurrently (max_in_flight={max_in_flight[0]}) — not parallel"
        )

    @pytest.mark.asyncio
    async def test_immediate_poll_filters_to_matching_repo(self):
        """handle_immediate_poll narrows the set to workspaces whose
        parsed (owner, repo) exactly matches the webhook's repo.

        Workspaces for a different repo — even one whose URL is a suffix
        of the target (wrapper/example-org/example-repo), or a
        different host — must NOT be polled.
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services.vcs_poller import handle_immediate_poll

        # 4 workspaces: 2 match, 2 do not. Rows are (id, url, provider)
        # — provider lets us avoid parsing a gitlab URL with github's
        # parser and vice-versa.
        matched_https = uuid.uuid4()
        matched_ssh = uuid.uuid4()
        other_repo = uuid.uuid4()
        suffix_collision = uuid.uuid4()

        rows = [
            (matched_https, "https://github.com/example-org/example-repo", "github"),
            (matched_ssh, "git@github.com:example-org/example-repo.git", "github"),
            (other_repo, "https://github.com/example-org/other-repo", "github"),
            (
                suffix_collision,
                "https://github.com/wrapper/example-org/example-repo",
                "github",
            ),
        ]

        mock_db = AsyncMock()
        select_result = MagicMock()
        select_result.all = MagicMock(return_value=rows)
        mock_db.execute = AsyncMock(return_value=select_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        polled: list[uuid.UUID] = []

        async def fake_owned_poll(ws_id, semaphore, cache=None, meta=None, paths_unions=None):
            polled.append(ws_id)

        with (
            patch("terrapod.services.vcs_poller.get_db_session", return_value=mock_ctx),
            patch(
                "terrapod.services.vcs_poller._poll_workspace_owned",
                side_effect=fake_owned_poll,
            ),
            patch(
                "terrapod.services.vcs_poller._compute_paths_unions",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await handle_immediate_poll({"repo": "example-org/example-repo"})

        # Only the two exact-match workspaces were polled.
        assert sorted(polled) == sorted([matched_https, matched_ssh])

    @pytest.mark.asyncio
    async def test_immediate_poll_underscore_repo_is_exact(self):
        """A repo named `my_repo` must match only `my_repo`, not `myXrepo`.
        Exact (owner, repo) comparison makes this trivial — this test
        guards against regressing to a LIKE-based filter.
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services.vcs_poller import handle_immediate_poll

        matched = uuid.uuid4()
        collision = uuid.uuid4()

        rows = [
            (matched, "https://github.com/ns/my_repo_v2", "github"),
            (collision, "https://github.com/ns/myXrepoXv2", "github"),
        ]

        mock_db = AsyncMock()
        select_result = MagicMock()
        select_result.all = MagicMock(return_value=rows)
        mock_db.execute = AsyncMock(return_value=select_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        polled: list[uuid.UUID] = []

        async def fake_owned_poll(ws_id, semaphore, cache=None, meta=None, paths_unions=None):
            polled.append(ws_id)

        with (
            patch("terrapod.services.vcs_poller.get_db_session", return_value=mock_ctx),
            patch(
                "terrapod.services.vcs_poller._poll_workspace_owned",
                side_effect=fake_owned_poll,
            ),
            patch(
                "terrapod.services.vcs_poller._compute_paths_unions",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await handle_immediate_poll({"repo": "ns/my_repo_v2"})

        assert polled == [matched]

    @pytest.mark.asyncio
    async def test_immediate_poll_does_not_cross_providers(self):
        """A GitHub webhook for `ns/repo` must not match a GitLab
        workspace that tracks a same-slugged `gitlab.com/ns/repo` —
        the two providers' URL parsers each accept the other's shape.
        Filter must scope to the webhook source's provider.
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services.vcs_poller import handle_immediate_poll

        github_match = uuid.uuid4()

        # DB would normally apply the VCSConnection.provider == "github"
        # filter in the query itself; we mock the DB so we simulate by
        # only returning github rows.
        rows = [(github_match, "https://github.com/ns/repo", "github")]

        mock_db = AsyncMock()
        select_result = MagicMock()
        select_result.all = MagicMock(return_value=rows)
        mock_db.execute = AsyncMock(return_value=select_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        polled: list[uuid.UUID] = []

        async def fake_owned_poll(ws_id, semaphore, cache=None, meta=None, paths_unions=None):
            polled.append(ws_id)

        with (
            patch("terrapod.services.vcs_poller.get_db_session", return_value=mock_ctx),
            patch(
                "terrapod.services.vcs_poller._poll_workspace_owned",
                side_effect=fake_owned_poll,
            ),
            patch(
                "terrapod.services.vcs_poller._compute_paths_unions",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            await handle_immediate_poll({"repo": "ns/repo", "provider": "github"})

        # Find the workspace-id select among the emitted statements (the
        # first `execute` is now the autodiscovery-rule fetch added by
        # #283; the workspace-id select comes after).
        provider_filtered = [
            str(call[0][0].compile(compile_kwargs={"literal_binds": True}))
            for call in mock_db.execute.await_args_list
            if "workspaces" in str(call[0][0])
            and "vcs_connections.provider"
            in str(call[0][0].compile(compile_kwargs={"literal_binds": True}))
        ]
        assert provider_filtered, (
            "expected at least one workspace SELECT with vcs_connections.provider filter"
        )
        assert "vcs_connections.provider = 'github'" in provider_filtered[0]
        assert polled == [github_match]

    @pytest.mark.asyncio
    async def test_immediate_poll_parses_workspace_url_with_its_own_provider(self):
        """Even if both workspaces' URLs look parseable by github's
        parser, only the github-connected one matches a github webhook.
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        github_ws = uuid.uuid4()
        gitlab_ws = uuid.uuid4()

        # In prod the provider filter in SQL would exclude the gitlab row
        # entirely; include both here to prove the Python-side parser
        # dispatch would also behave correctly if a gitlab row leaked in.
        rows = [
            (github_ws, "https://github.com/ns/repo", "github"),
            (gitlab_ws, "https://gitlab.com/ns/repo", "gitlab"),
        ]

        mock_db = AsyncMock()
        select_result = MagicMock()
        select_result.all = MagicMock(return_value=rows)
        mock_db.execute = AsyncMock(return_value=select_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        # Call the inner helper directly (bypassing handle_immediate_poll's
        # SQL-side provider filter) so we can verify the Python-side
        # parser dispatch behaves correctly on a mixed row set.
        from terrapod.services.vcs_poller import _select_workspace_ids

        with patch("terrapod.services.vcs_poller.get_db_session", return_value=mock_ctx):
            async with mock_ctx as fake_db:
                result = await _select_workspace_ids(fake_db, repo="ns/repo")

        # Both rows parse via their own provider; both have slug ns/repo;
        # both match. This demonstrates the dispatch is by workspace
        # provider, not by a single hard-coded parser.
        assert sorted(result) == sorted([github_ws, gitlab_ws])

    @pytest.mark.asyncio
    async def test_unknown_provider_is_warned_and_skipped(self):
        """An unknown provider row must NOT silently fall through to the
        github parser. The defensive warn-and-skip is dead code today
        (the DB only holds github/gitlab), but it's the tripwire that
        catches a future provider addition without a dispatch update.
        """
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services.vcs_poller import _select_workspace_ids

        bitbucket_ws = uuid.uuid4()
        rows = [(bitbucket_ws, "https://bitbucket.org/ns/repo", "bitbucket")]

        mock_db = AsyncMock()
        select_result = MagicMock()
        select_result.all = MagicMock(return_value=rows)
        mock_db.execute = AsyncMock(return_value=select_result)

        with patch("terrapod.services.vcs_poller.logger.warning") as mock_warn:
            result = await _select_workspace_ids(mock_db, repo="ns/repo")

        assert result == []
        mock_warn.assert_called_once()
        # The warning should identify the offending provider.
        call_kwargs = mock_warn.call_args.kwargs
        assert call_kwargs.get("provider") == "bitbucket"

    @pytest.mark.asyncio
    async def test_immediate_poll_no_matches_returns_quickly(self):
        """When no workspaces match the repo, nothing is polled."""
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from terrapod.services.vcs_poller import handle_immediate_poll

        mock_db = AsyncMock()
        select_result = MagicMock()
        # A workspace exists for a different repo; it must not be polled.
        select_result.all = MagicMock(
            return_value=[(uuid.uuid4(), "https://github.com/someone/else", "github")]
        )
        mock_db.execute = AsyncMock(return_value=select_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("terrapod.services.vcs_poller.get_db_session", return_value=mock_ctx),
            patch("terrapod.services.vcs_poller._poll_workspace_owned") as mock_poll,
        ):
            await handle_immediate_poll({"repo": "nobody/has-this"})

        mock_poll.assert_not_called()


class TestDescribeVCSError:
    """#1089 — the error a workspace reports must name the actual cause.

    Both providers call `raise_for_status()`, so the status and rate-limit
    headers are already on the exception. They were being discarded, which is
    how a provider-wide rate limit came out as a flat per-workspace message.
    """

    def _status_error(self, status: int, headers: dict[str, str] | None = None):
        import httpx

        request = httpx.Request("GET", "https://api.github.com/repos/org/repo")
        response = httpx.Response(status, headers=headers or {}, request=request)
        return httpx.HTTPStatusError("err", request=request, response=response)

    def test_github_rate_limit_names_the_rate_limit(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        msg = describe_vcs_error(
            self._status_error(403, {"x-ratelimit-remaining": "0"}),
        )
        assert "rate limit" in msg.lower()
        assert "403" in msg

    def test_gitlab_spelling_of_the_rate_limit_headers(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        # GitLab spells them RateLimit-*; httpx headers are case-insensitive so
        # one lookup must cover both providers.
        msg = describe_vcs_error(self._status_error(429, {"ratelimit-remaining": "0"}))
        assert "rate limit" in msg.lower()

    def test_retry_after_is_surfaced(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        msg = describe_vcs_error(self._status_error(429, {"retry-after": "60"}))
        assert "60" in msg

    def test_reset_epoch_rendered_as_a_duration(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        # An absolute epoch is useless in a UI banner; the operator wants "how long".
        future = int(datetime.now(UTC).timestamp()) + 300
        msg = describe_vcs_error(
            self._status_error(
                403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(future)}
            )
        )
        assert "resets in" in msg

    def test_auth_failure_is_not_reported_as_a_rate_limit(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        # A 401 has no rate-limit headers — it must not be swept into that branch,
        # which would send triage in exactly the wrong direction.
        msg = describe_vcs_error(self._status_error(401))
        assert "rate limit" not in msg.lower()
        assert "401" in msg

    def test_403_without_rate_limit_headers_stays_a_403(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        # A plain permission denial also returns 403. Only the headers distinguish it.
        msg = describe_vcs_error(self._status_error(403))
        assert "rate limit" not in msg.lower()
        assert "403" in msg

    def test_timeout_says_so(self):
        import httpx

        from terrapod.services.vcs_poller import describe_vcs_error

        msg = describe_vcs_error(httpx.ConnectTimeout("timed out"))
        assert "timed out" in msg.lower()

    def test_transport_error_says_unreachable(self):
        import httpx

        from terrapod.services.vcs_poller import describe_vcs_error

        msg = describe_vcs_error(httpx.ConnectError("no route to host"))
        assert "cannot reach" in msg.lower()

    def test_plain_exception_keeps_its_own_message(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        assert describe_vcs_error(Exception("403 Forbidden")) == "403 Forbidden"

    def test_message_less_exception_falls_back_to_its_type(self):
        from terrapod.services.vcs_poller import describe_vcs_error

        assert describe_vcs_error(KeyError()) == "KeyError"


class TestPollAttemptedAt:
    """#1089 — a stalled workspace must be detectable from the API alone.

    `vcs_last_polled_at` only advances on success, so on its own it cannot
    distinguish "not due yet" from "failing every cycle".
    """

    @patch("terrapod.services.vcs_poller._poll_workspace_prs")
    @patch("terrapod.services.vcs_poller._poll_workspace_branch")
    @patch("terrapod.services.vcs_poller._resolve_branch")
    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_stamped_on_success(self, mock_parse, mock_resolve, mock_branch, mock_prs):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace()
        mock_parse.return_value = ("org", "repo")
        mock_resolve.return_value = "main"
        mock_branch.return_value = None
        mock_prs.return_value = None
        mock_db = AsyncMock()
        mock_db.get.return_value = _mock_connection()

        await _poll_workspace(mock_db, ws)

        assert ws.vcs_last_attempted_at is not None

    @patch("terrapod.services.vcs_poller._poll_workspace_prs")
    @patch("terrapod.services.vcs_poller._poll_workspace_branch")
    @patch("terrapod.services.vcs_poller._resolve_branch")
    @patch("terrapod.services.vcs_poller._parse_repo_url")
    async def test_stamped_on_failure_too(self, mock_parse, mock_resolve, mock_branch, mock_prs):
        from terrapod.services.vcs_poller import _poll_workspace

        ws = _mock_workspace()
        mock_parse.return_value = ("org", "repo")
        mock_resolve.return_value = "main"
        mock_branch.side_effect = Exception("boom")
        mock_db = AsyncMock()
        mock_db.get.return_value = _mock_connection()

        await _poll_workspace(mock_db, ws)

        # This is the point: the attempt is recorded even though the poll failed
        # and `vcs_last_polled_at` never moved.
        assert ws.vcs_last_attempted_at is not None
        assert ws.vcs_last_polled_at is None

    async def test_stamped_before_the_connection_lookup_can_fail(self):
        from terrapod.services.vcs_poller import _poll_workspace

        # An inactive connection returns early. The attempt must still be on record
        # — otherwise the earliest failures are exactly the invisible ones.
        ws = _mock_workspace()
        conn = _mock_connection()
        conn.status = "inactive"
        mock_db = AsyncMock()
        mock_db.get.return_value = conn

        await _poll_workspace(mock_db, ws)

        assert ws.vcs_last_attempted_at is not None


class TestPollFailureSurvivesRollback:
    """#1089 — the rollback used to discard the very error it had just recorded.

    That is why a stalled workspace could report `vcs-last-error: null`: the
    poll set the error, something later in the transaction failed, and the
    rollback took the error record down with it.
    """

    @patch("terrapod.services.vcs_poller._record_poll_failure")
    @patch("terrapod.services.vcs_poller._poll_workspace")
    @patch("terrapod.services.vcs_poller.get_db_session")
    async def test_failure_is_re_recorded_after_rollback(
        self, mock_session, mock_poll, mock_record
    ):
        import contextlib

        from terrapod.services.vcs_poller import _poll_workspace_owned

        ws = _mock_workspace()
        db = AsyncMock()
        db.get.return_value = ws

        @contextlib.asynccontextmanager
        async def _session():
            yield db

        mock_session.side_effect = lambda: _session()
        mock_poll.side_effect = Exception("commit blew up")

        await _poll_workspace_owned(ws.id, asyncio.Semaphore(1), None, {})

        db.rollback.assert_awaited()
        mock_record.assert_awaited_once()
        # And it re-records the real cause, not a placeholder.
        assert "commit blew up" in mock_record.await_args.args[1]

    @patch("terrapod.services.vcs_poller.get_db_session")
    async def test_record_poll_failure_writes_in_its_own_transaction(self, mock_session):
        import contextlib

        from terrapod.services.vcs_poller import _record_poll_failure

        db = AsyncMock()

        @contextlib.asynccontextmanager
        async def _session():
            yield db

        mock_session.side_effect = lambda: _session()

        await _record_poll_failure(uuid.uuid4(), "rate limited")

        # A targeted UPDATE, committed — it must not depend on the session that
        # just failed, nor on any state that was rolled back.
        db.execute.assert_awaited_once()
        db.commit.assert_awaited_once()

    @patch("terrapod.services.vcs_poller.get_db_session")
    async def test_record_poll_failure_never_raises(self, mock_session):
        from terrapod.services.vcs_poller import _record_poll_failure

        # If the database is what is failing there is nowhere to record anything,
        # but that must not take the poll cycle down with it.
        mock_session.side_effect = Exception("database is down")

        await _record_poll_failure(uuid.uuid4(), "whatever")


class TestMetadataDeduplication:
    """#1096 — workspaces sharing a repo must not each re-ask the provider.

    The cache lives in `vcs_metadata_cache` and is unit-tested there. These
    assert the poller's thin provider wrappers are actually wired to it — a
    cache nothing routes through would save nothing.
    """

    async def test_branch_sha_deduped_across_workspaces(self):
        from terrapod.services.vcs_metadata_cache import VCSMetadataCache
        from terrapod.services.vcs_poller import _get_branch_sha

        conn = _mock_connection()
        conn.id = uuid.uuid4()
        meta = VCSMetadataCache()

        with patch(
            "terrapod.services.vcs_poller._provider_get_branch_sha",
            new_callable=AsyncMock,
            return_value="deadbeef",
        ) as prov:
            # Eleven workspaces on one repo+branch, as in a real monorepo estate.
            for _ in range(11):
                assert await _get_branch_sha(conn, "org", "repo", "main", meta) == "deadbeef"

        assert prov.await_count == 1

    async def test_open_prs_deduped_across_workspaces(self):
        from terrapod.services.vcs_metadata_cache import VCSMetadataCache
        from terrapod.services.vcs_poller import _list_open_prs

        conn = _mock_connection()
        conn.id = uuid.uuid4()
        meta = VCSMetadataCache()

        with patch(
            "terrapod.services.github_service.list_open_pull_requests",
            new_callable=AsyncMock,
            return_value=[],
        ) as prov:
            for _ in range(11):
                assert await _list_open_prs(conn, "org", "repo", "main", meta) == []

        assert prov.await_count == 1

    async def test_default_branch_deduped_across_workspaces(self):
        from terrapod.services.vcs_metadata_cache import VCSMetadataCache
        from terrapod.services.vcs_poller import _get_default_branch

        conn = _mock_connection()
        conn.id = uuid.uuid4()
        meta = VCSMetadataCache()

        with patch(
            "terrapod.services.vcs_poller._provider_get_default_branch",
            new_callable=AsyncMock,
            return_value="main",
        ) as prov:
            for _ in range(5):
                assert await _get_default_branch(conn, "org", "repo", meta) == "main"

        assert prov.await_count == 1

    async def test_without_a_cache_behaviour_is_unchanged(self):
        """One-shot callers (UI-queued runs, module-impact) pass no cache."""
        from terrapod.services.vcs_poller import _get_branch_sha

        conn = _mock_connection()
        conn.id = uuid.uuid4()

        with patch(
            "terrapod.services.vcs_poller._provider_get_branch_sha",
            new_callable=AsyncMock,
            return_value="deadbeef",
        ) as prov:
            for _ in range(3):
                await _get_branch_sha(conn, "org", "repo", "main")

        assert prov.await_count == 3, "no cache passed -> no caching, exactly as before"

    async def test_different_branches_are_not_conflated(self):
        from terrapod.services.vcs_metadata_cache import VCSMetadataCache
        from terrapod.services.vcs_poller import _get_branch_sha

        conn = _mock_connection()
        conn.id = uuid.uuid4()
        meta = VCSMetadataCache()

        async def by_branch(_c, _o, _r, branch):
            return f"sha-of-{branch}"

        with patch(
            "terrapod.services.vcs_poller._provider_get_branch_sha",
            new=AsyncMock(side_effect=by_branch),
        ) as prov:
            assert await _get_branch_sha(conn, "org", "repo", "main", meta) == "sha-of-main"
            assert await _get_branch_sha(conn, "org", "repo", "develop", meta) == "sha-of-develop"

        assert prov.await_count == 2


class TestPollCycleFailuresAreNotSilent:
    """#1096 — a poll cycle must never fail silently.

    `asyncio.gather(return_exceptions=True)` keeps one bad workspace from
    killing the cycle, which is right. But it also swallowed defects in the
    poller itself: while building the metadata cache, a stale call signature
    made every poll raise TypeError, ZERO workspaces were polled, and the cycle
    still reported success. For VCS change detection that is the worst possible
    failure — silence is indistinguishable from "nothing changed".
    """

    def test_partial_failure_is_logged(self):
        from terrapod.services.vcs_poller import _log_cycle_failures

        ids = [uuid.uuid4() for _ in range(3)]
        with patch("terrapod.services.vcs_poller.logger") as log:
            _log_cycle_failures([None, RuntimeError("boom"), None], ids)

        assert log.error.called, "a failed workspace poll must be logged, not dropped"
        # Partial failure is not the total-outage alarm.
        msgs = [c.args[0] for c in log.error.call_args_list]
        assert not any("change detection is down" in m for m in msgs)

    def test_total_failure_raises_the_louder_alarm(self):
        from terrapod.services.vcs_poller import _log_cycle_failures

        ids = [uuid.uuid4() for _ in range(3)]
        with patch("terrapod.services.vcs_poller.logger") as log:
            _log_cycle_failures([TypeError("bad signature")] * 3, ids)

        msgs = [c.args[0] for c in log.error.call_args_list]
        assert any("change detection is down" in m for m in msgs), (
            "every workspace failing is an outage and must say so explicitly"
        )

    def test_clean_cycle_logs_nothing(self):
        from terrapod.services.vcs_poller import _log_cycle_failures

        with patch("terrapod.services.vcs_poller.logger") as log:
            _log_cycle_failures([None, None], [uuid.uuid4(), uuid.uuid4()])

        assert not log.error.called

    def test_no_workspaces_is_not_an_outage(self):
        """An empty estate polls nothing; that is not a failure."""
        from terrapod.services.vcs_poller import _log_cycle_failures

        with patch("terrapod.services.vcs_poller.logger") as log:
            _log_cycle_failures([], [])

        assert not log.error.called
