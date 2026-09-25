"""Unit tests for the policy VCS poller."""

import io
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.policy_vcs_poller import (
    _classify,
    _extract_policy_files,
    _sync_policy_set,
    policy_vcs_poll_cycle,
)

POLICY = 'package terrapod\ndeny contains msg if { false\n  msg := "no" }'


def _make_tarball(files: dict[str, str]) -> bytes:
    """Create an in-memory gzipped tarball with the given path->content mapping."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestExtractPolicyFiles:
    """Extraction keeps every filename INTACT, extension included (#1842).

    It used to return `{name_without_extension: content}` and take `.rego`
    alone, which is why a `data.yaml` beside the policies could not be seen at
    all. Naming the policy is now `_classify`'s job — see `TestClassify` — and
    is tested there rather than here.
    """

    def test_extracts_from_policy_path(self):
        archive = _make_tarball(
            {
                "repo-abc123/policies/deny_s3.rego": 'package terrapod\ndeny contains msg if { false\n  msg := "no" }',
                "repo-abc123/policies/warn_tags.rego": "package terrapod\nwarn contains msg if { false }",
                "repo-abc123/policies/sub/nested.rego": "package terrapod\n# nested should be excluded",
                "repo-abc123/README.md": "# Policies",
            }
        )
        result, _skipped = _extract_policy_files(archive, "policies")
        assert "deny_s3.rego" in result
        assert "warn_tags.rego" in result
        assert "sub/nested.rego" not in result
        assert "nested.rego" not in result
        assert "README.md" not in result

    def test_extracts_from_root_when_path_empty(self):
        archive = _make_tarball(
            {
                "repo-abc123/main.rego": "package terrapod\ndeny contains msg if { false }",
                "repo-abc123/sub/other.rego": "package terrapod\n# sub should be excluded",
            }
        )
        result, _skipped = _extract_policy_files(archive, "")
        assert "main.rego" in result
        assert "other.rego" not in result

    def test_keeps_the_extension_on_the_key(self):
        """The extension is what tells OPA how to load a file and what
        separates a helper from a data file, so extraction must not strip it."""
        archive = _make_tarball(
            {
                "repo-abc123/my-policy.rego": "package terrapod\ndeny contains msg if { false }",
            }
        )
        result, _skipped = _extract_policy_files(archive, "")
        assert result.keys() == {"my-policy.rego"}

    def test_takes_data_files_and_still_ignores_everything_else(self):
        """`data.json` used to be dropped here — the reason a set could not
        carry data at all. A README beside the policies is still ignored."""
        archive = _make_tarball(
            {
                "repo-abc123/policies/valid.rego": "package terrapod\ndeny contains msg if { false }",
                "repo-abc123/policies/data.json": "{}",
                "repo-abc123/policies/data.yaml": "a: 1",
                "repo-abc123/policies/data.yml": "b: 2",
                "repo-abc123/policies/README.md": "# docs",
                "repo-abc123/policies/Makefile": "all:",
            }
        )
        result, _skipped = _extract_policy_files(archive, "policies")
        assert sorted(result) == ["data.json", "data.yaml", "data.yml", "valid.rego"]

    def test_handles_trailing_slash_in_path(self):
        archive = _make_tarball(
            {
                "repo-abc123/policies/test.rego": "package terrapod\ndeny contains msg if { false }",
            }
        )
        result, _skipped = _extract_policy_files(archive, "policies/")
        assert "test.rego" in result

    def test_empty_archive_returns_empty(self):
        archive = _make_tarball({})
        result, _skipped = _extract_policy_files(archive, "policies")
        assert result == {}

    def test_no_matching_path_returns_empty(self):
        archive = _make_tarball(
            {
                "repo-abc123/other-dir/test.rego": "package terrapod\ndeny contains msg if { false }",
            }
        )
        result, _skipped = _extract_policy_files(archive, "policies")
        assert result == {}

    def test_rejects_absolute_path_traversal(self):
        archive = _make_tarball(
            {
                "/etc/passwd.rego": "package terrapod\n",
            }
        )
        result, _skipped = _extract_policy_files(archive, "")
        assert result == {}

    def test_rejects_dotdot_traversal(self):
        archive = _make_tarball(
            {
                "../../../etc/shadow.rego": "package terrapod\n",
            }
        )
        result, _skipped = _extract_policy_files(archive, "")
        assert result == {}

    def test_rejects_embedded_dotdot(self):
        archive = _make_tarball(
            {
                "repo-abc123/../../../etc/passwd.rego": "package terrapod\n",
            }
        )
        result, _skipped = _extract_policy_files(archive, "")
        assert result == {}

    def test_traversal_is_rejected_for_data_files_too(self):
        """The suffix list grew, so the traversal guard has to cover the new
        suffixes as well — it sits before the suffix check, and this pins it."""
        archive = _make_tarball({"repo-abc123/../../../etc/evil.yaml": "a: 1"})
        assert _extract_policy_files(archive, "")[0] == {}


class TestClassify:
    """Naming a policy moved here from extraction (#1842)."""

    def test_a_policy_keeps_the_name_it_has_always_had(self):
        """Extension-stripped, because that name is what the UI, the results
        and the API call a policy — renaming them would break every consumer."""
        policies, support = _classify(
            {"my-policy.rego": "package terrapod\ndeny contains msg if { false }"}
        )
        assert policies.keys() == {"my-policy"}
        assert support == {}


# ── _sync_policy_set tests ──────────────────────────────────────────────


def _mock_policy_set(**overrides):
    ps = MagicMock()
    ps.id = overrides.get("id", uuid.uuid4())
    ps.name = overrides.get("name", "test-set")
    ps.source = "vcs"
    ps.vcs_repo_url = overrides.get("vcs_repo_url", "https://github.com/org/policies")
    ps.vcs_branch = overrides.get("vcs_branch", "main")
    ps.policy_path = overrides.get("policy_path", "policies")
    ps.vcs_last_commit_sha = overrides.get("vcs_last_commit_sha", "")
    ps.vcs_last_synced_at = None
    ps.vcs_last_error = None
    ps.policies = overrides.get("policies", [])
    conn = MagicMock()
    conn.provider = "github"
    ps.vcs_connection = overrides.get("vcs_connection", conn)
    return ps


def _mock_policy(name, rego="package terrapod\n"):
    p = MagicMock()
    p.name = name
    p.rego = rego
    p.updated_at = None
    return p


_PATCH_PREFIX = "terrapod.services.policy_vcs_poller"


class TestSyncPolicySet:
    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_skips_when_sha_unchanged(self, mock_parse, mock_sha):
        """No work done if branch SHA matches vcs_last_commit_sha."""
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "abc123"

        ps = _mock_policy_set(vcs_last_commit_sha="abc123")
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert ps.vcs_last_error is None

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_force_re_reads_an_unchanged_head(self, mock_parse, mock_sha, mock_download):
        """The backfill path. Same SHA, but the files are read anyway.

        This is what makes enabling `shared_evaluation` on an existing set
        work: `support_files` has no commit coming to populate it.
        """
        archive = _make_tarball(
            {
                "repo-abc123/policies/net.rego": POLICY,
                "repo-abc123/policies/data.yaml": "approved_cidrs: []",
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "same-sha"
        mock_download.return_value = archive

        ps = _mock_policy_set(vcs_last_commit_sha="same-sha")
        ps.support_files = {}
        db = AsyncMock()

        await _sync_policy_set(db, ps, force=True)

        assert ps.support_files == {"data.yaml": "approved_cidrs: []"}
        mock_download.assert_awaited_once()

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_without_force_an_unchanged_head_still_short_circuits(
        self, mock_parse, mock_sha, mock_download
    ):
        """The periodic poller must NOT re-download every cycle."""
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "same-sha"

        ps = _mock_policy_set(vcs_last_commit_sha="same-sha")
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        mock_download.assert_not_awaited()

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_a_skipped_file_is_reported_rather_than_passing_as_clean(
        self, mock_parse, mock_sha, mock_download
    ):
        """A skip can DELETE an enforced policy, so it cannot read as success.

        `s3_test.rego` is skipped as a fixture. If a policy of that name had
        been synced before, the reconcile removes its row — and reporting
        `vcs_last_error = None` left a mandatory set looking green while the
        rule it enforced was gone.
        """
        archive = _make_tarball(
            {
                "repo-abc123/policies/net.rego": POLICY,
                "repo-abc123/policies/s3_test.rego": POLICY,
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "new-sha"
        mock_download.return_value = archive

        ps = _mock_policy_set(vcs_last_commit_sha="old-sha")
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert ps.vcs_last_error is not None, "a partial sync reported clean"
        assert "s3_test.rego" in ps.vcs_last_error
        assert ps.vcs_last_commit_sha == "new-sha", "the policies that DID sync still land"

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_sets_error_when_branch_not_found(self, mock_parse, mock_sha):
        """vcs_last_error is set when the branch doesn't exist."""
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = None

        ps = _mock_policy_set(vcs_branch="nonexistent")
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert "not found" in ps.vcs_last_error

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_inserts_new_policies(self, mock_parse, mock_sha, mock_download):
        """New .rego files are added as Policy rows."""
        archive = _make_tarball(
            {
                "repo-abc123/policies/new_policy.rego": "package terrapod\ndeny contains msg if { false }",
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "def456"
        mock_download.return_value = archive

        ps = _mock_policy_set(policies=[])
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.name == "new_policy"
        assert ps.vcs_last_commit_sha == "def456"
        assert ps.vcs_last_error is None

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_updates_modified_policies(self, mock_parse, mock_sha, mock_download):
        """Existing policies with changed content get updated."""
        new_rego = "package terrapod\ndeny contains msg if { true }"
        archive = _make_tarball(
            {
                "repo-abc123/policies/existing.rego": new_rego,
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "def456"
        mock_download.return_value = archive

        existing_policy = _mock_policy("existing", rego="package terrapod\nold content")
        ps = _mock_policy_set(policies=[existing_policy])
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert existing_policy.rego == new_rego
        db.add.assert_not_called()  # update, not insert

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_deletes_removed_policies(self, mock_parse, mock_sha, mock_download):
        """Policies whose .rego files no longer exist get deleted."""
        archive = _make_tarball(
            {
                "repo-abc123/policies/kept.rego": "package terrapod\ndeny contains msg if { false }",
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "def456"
        mock_download.return_value = archive

        kept = _mock_policy("kept")
        removed = _mock_policy("old_policy")
        ps = _mock_policy_set(policies=[kept, removed])
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        db.delete.assert_called_once_with(removed)

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_sets_error_on_exception(self, mock_parse, mock_sha):
        """Exceptions during sync are caught and stored in vcs_last_error."""
        mock_parse.return_value = ("org", "policies")
        mock_sha.side_effect = RuntimeError("network timeout")

        ps = _mock_policy_set()
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert "network timeout" in ps.vcs_last_error

    @pytest.mark.asyncio
    async def test_sets_error_when_connection_deleted(self):
        """vcs_last_error is set when the VCS connection is None (FK SET NULL)."""
        ps = _mock_policy_set(vcs_connection=None)
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert "VCS connection deleted" in ps.vcs_last_error

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._MAX_ARCHIVE_BYTES", 10)
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_sets_error_when_archive_too_large(self, mock_parse, mock_sha, mock_download):
        """vcs_last_error is set when archive exceeds size limit."""
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "def456"
        mock_download.side_effect = ValueError("Archive exceeds 0 MB limit (100 bytes)")

        ps = _mock_policy_set()
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert "exceeds" in ps.vcs_last_error or "limit" in ps.vcs_last_error

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_rego_with_wrong_package_is_not_a_policy(
        self, mock_parse, mock_sha, mock_download
    ):
        """It is kept as a support file rather than becoming a Policy row.

        Before #1842 it was dropped at sync. It is still not a policy — the
        package check is what stops an unrelated rego file in the directory
        being run as one — but a set may legitimately carry it as a helper."""
        archive = _make_tarball(
            {
                "repo-abc123/policies/good.rego": "package terrapod\ndeny contains msg if { false }",
                "repo-abc123/policies/bad_pkg.rego": "package aws.s3\ndeny contains msg if { false }",
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "new-sha"
        mock_download.return_value = archive

        ps = _mock_policy_set(vcs_last_commit_sha="old-sha")
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert db.add.called
        add_arg = db.add.call_args_list[0][0][0]
        assert add_arg.name == "good"
        assert len(db.add.call_args_list) == 1
        assert ps.support_files.keys() == {"bad_pkg.rego"}

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_a_verdictless_rego_becomes_a_support_file(
        self, mock_parse, mock_sha, mock_download
    ):
        """The shared helper the issue is about. It produces no verdict, so it
        is not a Policy — but it must reach the set, or it cannot be called."""
        archive = _make_tarball(
            {
                "repo-abc123/policies/good.rego": "package terrapod\ndeny contains msg if { false }",
                "repo-abc123/policies/helpers.rego": "package terrapod\nis_ok(x) if { x == 1 }",
                "repo-abc123/policies/data.yaml": "approved: []",
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "new-sha"
        mock_download.return_value = archive

        ps = _mock_policy_set(vcs_last_commit_sha="old-sha")
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert len(db.add.call_args_list) == 1
        assert db.add.call_args_list[0][0][0].name == "good"
        assert sorted(ps.support_files) == ["data.yaml", "helpers.rego"]

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_support_files_are_synced_whatever_the_flag_says(
        self, mock_parse, mock_sha, mock_download
    ):
        """Gating the sync on `shared_evaluation` would mean an operator who
        turns it on sees nothing change until the next poll, which reads as the
        feature being broken."""
        archive = _make_tarball(
            {
                "repo-abc123/policies/good.rego": "package terrapod\ndeny contains msg if { false }",
                "repo-abc123/policies/data.yaml": "approved: []",
            }
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "new-sha"
        mock_download.return_value = archive

        ps = _mock_policy_set(vcs_last_commit_sha="old-sha")
        ps.shared_evaluation = False
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert ps.support_files.keys() == {"data.yaml"}

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}._download_archive", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._get_branch_sha", new_callable=AsyncMock)
    @patch(f"{_PATCH_PREFIX}._parse_repo_url")
    async def test_a_deleted_support_file_stops_being_carried(
        self, mock_parse, mock_sha, mock_download
    ):
        """`support_files` is REPLACED, not merged — deleting a data file from
        the repo must stop the policies seeing its data. A merge would leave a
        removed allowlist silently in force."""
        archive = _make_tarball(
            {"repo-abc123/policies/good.rego": "package terrapod\ndeny contains msg if { false }"}
        )
        mock_parse.return_value = ("org", "policies")
        mock_sha.return_value = "new-sha"
        mock_download.return_value = archive

        ps = _mock_policy_set(vcs_last_commit_sha="old-sha")
        ps.support_files = {"data.yaml": "approved: [everything]"}
        db = AsyncMock()

        await _sync_policy_set(db, ps)

        assert ps.support_files == {}


# ── policy_vcs_poll_cycle tests ──────────────────────────────────────────


class TestPolicyVcsPollCycle:
    @pytest.mark.asyncio
    @patch("terrapod.services.policy_vcs_poller.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.services.policy_vcs_poller.get_db_session")
    async def test_enqueues_one_trigger_per_set(self, mock_get_db, mock_enqueue):
        """poll_cycle fans out one trigger per VCS policy set."""
        ps_id_1 = uuid.uuid4()
        ps_id_2 = uuid.uuid4()

        mock_db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [ps_id_1, ps_id_2]
        mock_db.execute = AsyncMock(return_value=result)
        mock_get_db.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        await policy_vcs_poll_cycle()

        assert mock_enqueue.call_count == 2
        payloads = [call.kwargs["payload"] for call in mock_enqueue.call_args_list]
        assert {"policy_set_id": str(ps_id_1)} in payloads
        assert {"policy_set_id": str(ps_id_2)} in payloads


# ── handle_policy_vcs_sync tests ───────────────────────────────────────────


class TestHandlePolicyVCSSync:
    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}.get_db_session")
    @patch(f"{_PATCH_PREFIX}._sync_policy_set", new_callable=AsyncMock)
    async def test_syncs_vcs_policy_set(self, mock_sync, mock_session):
        from terrapod.services.policy_vcs_poller import handle_policy_vcs_sync

        ps = _mock_policy_set()

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = ps
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()

        mock_session.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_policy_vcs_sync({"policy_set_id": str(ps.id)})

        mock_sync.assert_called_once_with(db, ps, force=False)
        db.commit.assert_called_once()

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}.get_db_session")
    @patch(f"{_PATCH_PREFIX}._sync_policy_set", new_callable=AsyncMock)
    async def test_an_explicit_sync_forces_a_re_read(self, mock_sync, mock_session):
        """The Sync button has to re-read a repository whose head has not moved.

        `support_files` arrived with #1842, so a set that predates it has `{}`
        and no commit coming to fill it — and the SHA check returns before the
        line that would. Without force, Sync did nothing and a set with shared
        evaluation on evaluated with no data, which passes a mandatory gate.
        """
        from terrapod.services.policy_vcs_poller import handle_policy_vcs_sync

        ps = _mock_policy_set()
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = ps
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        mock_session.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_policy_vcs_sync({"policy_set_id": str(ps.id), "force": True})

        mock_sync.assert_called_once_with(db, ps, force=True)

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}.get_db_session")
    @patch(f"{_PATCH_PREFIX}._sync_policy_set", new_callable=AsyncMock)
    async def test_skips_when_not_found(self, mock_sync, mock_session):
        from terrapod.services.policy_vcs_poller import handle_policy_vcs_sync

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        mock_session.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_policy_vcs_sync({"policy_set_id": str(uuid.uuid4())})

        mock_sync.assert_not_called()

    @pytest.mark.asyncio
    @patch(f"{_PATCH_PREFIX}.get_db_session")
    @patch(f"{_PATCH_PREFIX}._sync_policy_set", new_callable=AsyncMock)
    async def test_skips_inline_source(self, mock_sync, mock_session):
        from terrapod.services.policy_vcs_poller import handle_policy_vcs_sync

        ps = _mock_policy_set()
        ps.source = "inline"

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = ps
        db.execute = AsyncMock(return_value=result)

        mock_session.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await handle_policy_vcs_sync({"policy_set_id": str(ps.id)})

        mock_sync.assert_not_called()
