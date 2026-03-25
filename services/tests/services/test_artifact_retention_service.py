"""Tests for artifact retention and cleanup service."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.artifact_retention_service import (
    _cleanup_binary_cache,
    _cleanup_config_versions,
    _cleanup_module_overrides,
    _cleanup_provider_cache,
    _cleanup_run_artifacts,
    _cleanup_state_versions,
    artifact_retention_cycle,
)


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
):
    run = MagicMock()
    run.id = run_id or _make_uuid()
    run.workspace_id = workspace_id
    run.status = status
    run.created_at = created_at or datetime.now(UTC) - timedelta(days=120)
    run.configuration_version_id = cv_id
    run.module_overrides = module_overrides
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
        # 3 artifacts per run (plan log, apply log, plan output)
        assert deleted == 6
        assert storage.delete.call_count == 6

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
