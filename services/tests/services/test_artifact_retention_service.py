"""Tests for artifact retention and cleanup service."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.artifact_retention_service import (
    RUN_ARTIFACT_KEYS,
    _cleanup_binary_cache,
    _cleanup_config_versions,
    _cleanup_module_overrides,
    _cleanup_provider_cache,
    _cleanup_run_artifacts,
    _cleanup_state_versions,
    _cleanup_vcs_archives,
    artifact_retention_cycle,
)
from terrapod.storage.keys import cost_estimate_key, lock_file_key, plan_artifacts_key


def _make_uuid() -> uuid.UUID:
    return uuid.uuid4()


def _make_state_version(workspace_id, serial, sv_id=None):
    sv = MagicMock()
    sv.id = sv_id or _make_uuid()
    sv.workspace_id = workspace_id
    sv.serial = serial
    return sv


def _make_run(
    workspace_id,
    status="applied",
    created_at=None,
    cv_id=None,
    module_overrides=None,
    run_id=None,
    has_json_output=False,
    has_cost_estimate=False,
    plan_only=False,
):
    run = MagicMock()
    run.id = run_id or _make_uuid()
    run.workspace_id = workspace_id
    run.status = status
    run.created_at = created_at or datetime.now(UTC) - timedelta(days=120)
    run.configuration_version_id = cv_id
    run.module_overrides = module_overrides
    run.has_json_output = has_json_output
    # Every flag the sweep READS has to be set here, not left as a MagicMock:
    # an unset attribute is truthy, so the sweep would "reset" it and commit,
    # and `test_no_commit_when_no_flags_to_reset` would fail for a reason that
    # has nothing to do with what it is testing (#2148).
    run.has_cost_estimate = has_cost_estimate
    run.plan_only = plan_only
    return run


def _make_cv(workspace_id, created_at=None, cv_id=None):
    cv = MagicMock()
    cv.id = cv_id or _make_uuid()
    cv.workspace_id = workspace_id
    cv.created_at = created_at or datetime.now(UTC) - timedelta(days=120)
    return cv


def _make_cached_binary(tool, version, os_, arch, last_accessed_at=None):
    entry = MagicMock()
    entry.id = _make_uuid()
    entry.tool = tool
    entry.version = version
    entry.os = os_
    entry.arch = arch
    entry.last_accessed_at = last_accessed_at or datetime.now(UTC) - timedelta(days=60)
    return entry


def _make_cached_provider(
    hostname, namespace, type_, version, os_, arch, filename, last_accessed_at=None
):
    entry = MagicMock()
    entry.id = _make_uuid()
    entry.hostname = hostname
    entry.namespace = namespace
    entry.type = type_
    entry.version = version
    entry.os = os_
    entry.arch = arch
    entry.filename = filename
    entry.last_accessed_at = last_accessed_at or datetime.now(UTC) - timedelta(days=60)
    return entry


class _FakeResult:
    """Mimics SQLAlchemy result for scalars().all()."""

    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeTupleResult:
    """Mimics SQLAlchemy result for .all() returning tuples."""

    def __init__(self, tuples):
        self._tuples = tuples

    def all(self):
        return self._tuples


# ── _cleanup_state_versions ──────────────────────────────────────────


class TestCleanupStateVersions:
    @pytest.mark.asyncio
    async def test_skips_state_diverged_workspaces(self):
        """Workspaces with state_diverged=True should be skipped."""
        ws_id = _make_uuid()
        db = AsyncMock()
        storage = AsyncMock()

        # First query: workspaces with excess state versions
        db.execute.return_value = _FakeTupleResult([(ws_id, True)])  # state_diverged=True

        deleted = await _cleanup_state_versions(db, storage, keep=5, batch_size=100)
        assert deleted == 0
        storage.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_excess_state_versions(self):
        """Should delete state versions beyond the keep count."""
        ws_id = _make_uuid()
        excess_svs = [_make_state_version(ws_id, serial=i) for i in range(3)]

        db = AsyncMock()
        storage = AsyncMock()

        # First query: workspace with excess
        db.execute.side_effect = [
            _FakeTupleResult([(ws_id, False)]),
            _FakeResult(excess_svs),
        ]

        deleted = await _cleanup_state_versions(db, storage, keep=5, batch_size=100)
        assert deleted == 3
        assert storage.delete.call_count == 3
        assert db.delete.call_count == 3
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_workspaces_returns_zero(self):
        """When no workspaces have excess state versions."""
        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeTupleResult([])

        deleted = await _cleanup_state_versions(db, storage, keep=20, batch_size=100)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_respects_batch_size(self):
        """Should not exceed batch_size deletions."""
        ws_id = _make_uuid()
        excess_svs = [_make_state_version(ws_id, serial=i) for i in range(5)]

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.side_effect = [
            _FakeTupleResult([(ws_id, False)]),
            _FakeResult(excess_svs[:2]),  # limited by batch_size
        ]

        deleted = await _cleanup_state_versions(db, storage, keep=5, batch_size=2)
        assert deleted == 2

    @pytest.mark.asyncio
    async def test_storage_delete_failure_continues(self):
        """Storage delete failure should not abort the batch."""
        ws_id = _make_uuid()
        excess_svs = [_make_state_version(ws_id, serial=i) for i in range(2)]

        db = AsyncMock()
        storage = AsyncMock()
        storage.delete.side_effect = [Exception("storage error"), None]
        db.execute.side_effect = [
            _FakeTupleResult([(ws_id, False)]),
            _FakeResult(excess_svs),
        ]

        deleted = await _cleanup_state_versions(db, storage, keep=5, batch_size=100)
        assert deleted == 2  # both counted, even if storage delete failed


# ── _cleanup_run_artifacts ──────────────────────────────────────────


class TestCleanupRunArtifacts:
    @pytest.mark.asyncio
    async def test_deletes_artifacts_for_old_terminal_runs(self):
        ws_id = _make_uuid()
        runs = [_make_run(ws_id, status="applied"), _make_run(ws_id, status="errored")]

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult(runs)

        deleted = await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)

        # Derived from the sweep's own key set rather than written as a number
        # (#2021). It was `== 8` -- two runs times the four keys of the day --
        # and adding a key to `RUN_ARTIFACT_KEYS` broke it for no reason beyond
        # the arithmetic, which is the kind of failure that gets "fixed" by
        # bumping the literal and stops meaning anything.
        expected = len(runs) * len(RUN_ARTIFACT_KEYS)
        assert deleted == expected
        assert storage.delete.call_count == expected

        # And the keys themselves, which the count alone never asserted: every
        # key in the set, for every run. A count can be right while the sweep
        # deletes one run's artifacts twice.
        asked = {call.args[0] for call in storage.delete.await_args_list}
        for run in runs:
            for key_fn in RUN_ARTIFACT_KEYS:
                assert key_fn(str(ws_id), str(run.id)) in asked, key_fn.__name__

    @pytest.mark.asyncio
    async def test_the_per_host_event_partitions_are_deleted_too(self):
        """#1928's partitions, whose keys are DERIVED rather than fixed.

        They cannot be entries in `RUN_ARTIFACT_KEYS` -- its functions take only
        `(workspace_id, run_id)` and these need the host -- so nothing else in
        this sweep would reach them and they would be the one artifact that
        outlives its run for ever.

        The host set comes from the run's own `run_host_results` rows, which is
        what makes writing N objects per run safe: nothing in the tree deletes
        those rows and this sweep touches storage only, so they are still there
        however long afterwards this fires.
        """
        from terrapod.storage.keys import host_events_host_key

        ws_id = _make_uuid()
        run = _make_run(ws_id, status="applied")
        hosts = ["web1", "web2", "db1"]

        db = AsyncMock()
        storage = AsyncMock()
        # Scripted in call order: the run sweep, then this run's host rows.
        db.execute.side_effect = [_FakeResult([run]), _FakeResult(hosts)]

        deleted = await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)

        asked = {call.args[0] for call in storage.delete.await_args_list}
        for host in hosts:
            key = host_events_host_key(str(ws_id), str(run.id), host)
            assert key in asked, f"partition for {host} was not deleted"
        # And the count includes them, so a partition deleted without being
        # counted cannot read as nothing having happened.
        assert deleted == len(RUN_ARTIFACT_KEYS) + len(hosts)

    @pytest.mark.asyncio
    async def test_a_run_that_reported_no_hosts_asks_for_no_partitions(self):
        """No rows, no derived keys -- so a plan or apply costs nothing extra.

        The sweep runs over every terminal run in the deployment, the
        overwhelming majority of which are plans and applies with no host
        results at all, so this is the common path rather than an edge.
        """
        ws_id = _make_uuid()
        run = _make_run(ws_id, status="applied")

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.side_effect = [_FakeResult([run]), _FakeResult([])]

        deleted = await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)

        assert deleted == len(RUN_ARTIFACT_KEYS)
        assert storage.delete.await_count == len(RUN_ARTIFACT_KEYS)

    @pytest.mark.asyncio
    async def test_resets_has_json_output_flag_when_artifact_deleted(self):
        """After retention deletes a run's `.json-output`, the row must stop
        advertising the URL via `_plan_json` — i.e. `has_json_output` must
        flip back to False, and the change must be committed.
        """
        ws_id = _make_uuid()
        runs = [
            _make_run(ws_id, status="applied", has_json_output=True),
            _make_run(ws_id, status="applied", has_json_output=False),
        ]

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult(runs)

        await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)

        # The `True` run was reset; the `False` run was untouched.
        assert runs[0].has_json_output is False
        assert runs[1].has_json_output is False
        # One commit covers all flag changes in the batch.
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resets_has_cost_estimate_flag_when_artifact_deleted(self):
        """`has_cost_estimate` gates the run page's Cost tab and the
        `has-cost-estimate` attribute in the run JSON, so once the estimate is
        reaped the row must stop advertising it -- exactly as `has_json_output`
        has always done. It was missed when the cost estimate was never reaped
        at all (#2148); the flag and the reap arrive together."""
        ws_id = _make_uuid()
        runs = [
            _make_run(ws_id, status="applied", has_cost_estimate=True),
            _make_run(ws_id, status="applied", has_cost_estimate=False),
        ]

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult(runs)

        await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)

        assert runs[0].has_cost_estimate is False
        assert runs[1].has_cost_estimate is False
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_cost_estimate_and_lock_and_plan_artifacts_are_asked_for(self):
        """The three keys #2148 added, named individually.

        `len(RUN_ARTIFACT_KEYS)` above moves with the tuple, so it would stay
        green if one of these were dropped again while another was added. These
        are the keys that went years unreaped, so they get their own assertion.
        """
        ws_id = _make_uuid()
        run = _make_run(ws_id, status="applied")

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([run])

        await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)

        asked = {call.args[0] for call in storage.delete.await_args_list}
        for key_fn in (cost_estimate_key, lock_file_key, plan_artifacts_key):
            assert key_fn(str(ws_id), str(run.id)) in asked, key_fn.__name__

    @pytest.mark.asyncio
    async def test_no_commit_when_no_flags_to_reset(self):
        """If no run had `has_json_output=True`, retention must skip the
        commit — no point burning a transaction on no-ops.
        """
        ws_id = _make_uuid()
        runs = [_make_run(ws_id, status="applied", has_json_output=False, has_cost_estimate=False)]

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult(runs)

        await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)

        db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_old_runs_returns_zero(self):
        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([])

        deleted = await _cleanup_run_artifacts(db, storage, retention_days=90, batch_size=100)
        assert deleted == 0


# ── _cleanup_config_versions ─────────────────────────────────────────


class TestCleanupConfigVersions:
    @pytest.mark.asyncio
    async def test_deletes_old_unreferenced_cvs(self):
        ws_id = _make_uuid()
        cvs = [_make_cv(ws_id), _make_cv(ws_id)]

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult(cvs)

        deleted = await _cleanup_config_versions(db, storage, retention_days=90, batch_size=100)
        assert deleted == 2
        assert storage.delete.call_count == 2
        assert db.delete.call_count == 2
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_eligible_cvs_returns_zero(self):
        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([])

        deleted = await _cleanup_config_versions(db, storage, retention_days=90, batch_size=100)
        assert deleted == 0


# ── _cleanup_provider_cache ──────────────────────────────────────────


class TestCleanupProviderCache:
    @pytest.mark.asyncio
    async def test_deletes_stale_provider_cache_entries(self):
        entry = _make_cached_provider(
            "registry.terraform.io",
            "hashicorp",
            "aws",
            "5.0.0",
            "linux",
            "amd64",
            "terraform-provider-aws_5.0.0_linux_amd64.zip",
        )

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([entry])

        deleted = await _cleanup_provider_cache(db, storage, retention_days=30, batch_size=100)
        assert deleted == 1
        storage.delete.assert_called_once()
        db.delete.assert_called_once_with(entry)

    @pytest.mark.asyncio
    async def test_recently_accessed_entries_not_deleted(self):
        """Entries accessed within retention period should not be deleted."""
        db = AsyncMock()
        storage = AsyncMock()
        # No entries returned (query filters by last_accessed_at)
        db.execute.return_value = _FakeResult([])

        deleted = await _cleanup_provider_cache(db, storage, retention_days=30, batch_size=100)
        assert deleted == 0


# ── _cleanup_binary_cache ────────────────────────────────────────────


class TestCleanupBinaryCache:
    @pytest.mark.asyncio
    async def test_deletes_stale_binary_cache_entries(self):
        entry = _make_cached_binary("tofu", "1.8.0", "linux", "amd64")

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([entry])

        deleted = await _cleanup_binary_cache(db, storage, retention_days=30, batch_size=100)
        assert deleted == 1
        storage.delete.assert_called_once()
        db.delete.assert_called_once_with(entry)

    @pytest.mark.asyncio
    async def test_no_stale_entries_returns_zero(self):
        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([])

        deleted = await _cleanup_binary_cache(db, storage, retention_days=30, batch_size=100)
        assert deleted == 0


# ── _cleanup_module_overrides ────────────────────────────────────────


class TestCleanupModuleOverrides:
    @pytest.mark.asyncio
    async def test_deletes_override_storage_and_clears_jsonb(self):
        ws_id = _make_uuid()
        overrides = {
            "default/vpc/aws": "module_overrides/abc123/default/vpc/aws.tar.gz",
            "default/rds/aws": "module_overrides/abc123/default/rds/aws.tar.gz",
        }
        run = _make_run(ws_id, status="applied", module_overrides=overrides)

        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([run])

        deleted = await _cleanup_module_overrides(db, storage, retention_days=14, batch_size=100)
        assert deleted == 2
        assert storage.delete.call_count == 2
        assert run.module_overrides is None

    @pytest.mark.asyncio
    async def test_no_overrides_returns_zero(self):
        db = AsyncMock()
        storage = AsyncMock()
        db.execute.return_value = _FakeResult([])

        deleted = await _cleanup_module_overrides(db, storage, retention_days=14, batch_size=100)
        assert deleted == 0


# ── artifact_retention_cycle ─────────────────────────────────────────


class TestArtifactRetentionCycle:
    @pytest.mark.asyncio
    @patch("terrapod.services.artifact_retention_service.settings")
    @patch("terrapod.services.artifact_retention_service.get_logger")
    async def test_cycle_handles_per_category_errors(self, mock_get_logger, mock_settings):
        """Each category failure should not prevent other categories from running."""
        cfg = MagicMock()
        cfg.state_versions_keep = 20
        cfg.run_artifacts_retention_days = 90
        cfg.config_versions_retention_days = 90
        cfg.provider_cache_retention_days = 30
        cfg.binary_cache_retention_days = 30
        cfg.module_overrides_retention_days = 14
        cfg.batch_size = 100
        mock_settings.artifact_retention = cfg

        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger

        # Patch get_db_session to raise on first call, succeed on rest
        call_count = 0

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def mock_db_session():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("DB error")
            yield AsyncMock()

        with (
            patch(
                "terrapod.storage.get_storage",
                return_value=AsyncMock(),
            ),
            patch(
                "terrapod.services.artifact_retention_service._cleanup_state_versions",
                side_effect=Exception("state error"),
            ) as mock_sv,
            patch(
                "terrapod.services.artifact_retention_service._cleanup_run_artifacts",
                return_value=0,
            ) as mock_ra,
            patch(
                "terrapod.services.artifact_retention_service._cleanup_config_versions",
                return_value=0,
            ),
            patch(
                "terrapod.services.artifact_retention_service._cleanup_provider_cache",
                return_value=0,
            ),
            patch(
                "terrapod.services.artifact_retention_service._cleanup_binary_cache",
                return_value=0,
            ),
            patch(
                "terrapod.services.artifact_retention_service._cleanup_module_overrides",
                return_value=0,
            ),
        ):
            # Patch get_db_session to just return a mock
            @asynccontextmanager
            async def db_session():
                yield AsyncMock()

            with patch(
                "terrapod.db.session.get_db_session",
                side_effect=db_session,
            ):
                await artifact_retention_cycle()

            # State versions handler was called (and errored)
            mock_sv.assert_called_once()
            # Run artifacts handler was still called despite state version error
            mock_ra.assert_called_once()

    @pytest.mark.asyncio
    @patch("terrapod.services.artifact_retention_service.settings")
    async def test_cycle_skips_disabled_categories(self, mock_settings):
        """Categories with threshold=0 should be skipped entirely."""
        cfg = MagicMock()
        cfg.state_versions_keep = 0  # disabled
        cfg.run_artifacts_retention_days = 0  # disabled
        cfg.config_versions_retention_days = 0  # disabled
        cfg.provider_cache_retention_days = 0  # disabled
        cfg.binary_cache_retention_days = 0  # disabled
        cfg.module_overrides_retention_days = 0  # disabled
        cfg.batch_size = 100
        mock_settings.artifact_retention = cfg

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def db_session():
            yield AsyncMock()

        with (
            patch(
                "terrapod.storage.get_storage",
                return_value=AsyncMock(),
            ),
            patch(
                "terrapod.db.session.get_db_session",
                side_effect=db_session,
            ),
            patch(
                "terrapod.services.artifact_retention_service._cleanup_state_versions"
            ) as mock_sv,
            patch("terrapod.services.artifact_retention_service._cleanup_run_artifacts") as mock_ra,
        ):
            await artifact_retention_cycle()
            mock_sv.assert_not_called()
            mock_ra.assert_not_called()


# ── _cleanup_vcs_archives ────────────────────────────────────────────


def _make_object_meta(key: str, last_modified):
    """Build an ObjectMeta-shaped MagicMock for storage.list_prefix() returns."""
    meta = MagicMock()
    meta.key = key
    meta.last_modified = last_modified
    return meta


class TestCleanupVCSArchives:
    @pytest.mark.asyncio
    async def test_deletes_old_entries_only(self):
        now = datetime.now(UTC)
        old = _make_object_meta("vcs_archives/c1/owner/repo/old.tar.gz", now - timedelta(days=10))
        recent = _make_object_meta(
            "vcs_archives/c1/owner/repo/recent.tar.gz", now - timedelta(days=2)
        )
        storage = AsyncMock()
        storage.list_prefix = AsyncMock(return_value=[old, recent])
        storage.delete = AsyncMock()
        db = AsyncMock()

        deleted = await _cleanup_vcs_archives(db, storage, retention_days=7, batch_size=100)

        assert deleted == 1
        storage.delete.assert_awaited_once_with(old.key)

    @pytest.mark.asyncio
    async def test_evicts_oldest_first_when_capped_by_batch_size(self):
        now = datetime.now(UTC)
        ages = [now - timedelta(days=d) for d in (30, 20, 15, 10)]
        # All four are past the 7-day cutoff; with batch_size=2 we expect the
        # oldest two (days=30, days=20) deleted, the newer two left for next cycle.
        entries = [
            _make_object_meta(f"vcs_archives/c1/owner/repo/sha{i}.tar.gz", a)
            for i, a in enumerate(ages)
        ]
        storage = AsyncMock()
        storage.list_prefix = AsyncMock(return_value=entries)
        storage.delete = AsyncMock()
        db = AsyncMock()

        deleted = await _cleanup_vcs_archives(db, storage, retention_days=7, batch_size=2)

        assert deleted == 2
        deleted_keys = [c.args[0] for c in storage.delete.await_args_list]
        # Oldest first: sha0 (30 days), sha1 (20 days)
        assert deleted_keys == [entries[0].key, entries[1].key]

    @pytest.mark.asyncio
    async def test_returns_zero_when_list_prefix_fails(self):
        storage = AsyncMock()
        storage.list_prefix = AsyncMock(side_effect=RuntimeError("storage backend down"))
        storage.delete = AsyncMock()
        db = AsyncMock()

        deleted = await _cleanup_vcs_archives(db, storage, retention_days=7, batch_size=100)

        assert deleted == 0
        storage.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_individual_delete_failure_and_continues(self):
        now = datetime.now(UTC)
        old1 = _make_object_meta("vcs_archives/c1/repo/a.tar.gz", now - timedelta(days=10))
        old2 = _make_object_meta("vcs_archives/c1/repo/b.tar.gz", now - timedelta(days=9))
        storage = AsyncMock()
        storage.list_prefix = AsyncMock(return_value=[old1, old2])
        # First delete fails, second succeeds — we should still count the
        # successful one and proceed.
        storage.delete = AsyncMock(side_effect=[RuntimeError("transient"), None])
        db = AsyncMock()

        deleted = await _cleanup_vcs_archives(db, storage, retention_days=7, batch_size=100)

        assert deleted == 1
        assert storage.delete.await_count == 2
