"""Artifact retention and cleanup service.

Periodically removes old artifacts from object storage and the database
to prevent unbounded storage growth.  Registered as a periodic task with
the distributed scheduler — multi-replica safe.

Safety invariants:
  - Never delete the latest state version (highest serial per workspace).
  - Skip workspaces with state_diverged=True.
  - Only clean run artifacts for runs in terminal states.
  - Only clean config versions not referenced by any non-terminal run.
  - Cache entries are cleaned based on last_accessed_at, not cached_at.
  - All storage deletes are best-effort (catch + log, continue).
  - Each category is independently try/excepted.
"""

import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import (
    CachedBinary,
    CachedProviderPackage,
    ConfigurationVersion,
    Run,
    StateVersion,
    Workspace,
)
from terrapod.logging_config import get_logger
from terrapod.services.run_service import TERMINAL_STATES
from terrapod.storage.keys import (
    apply_log_key,
    binary_cache_key,
    config_version_key,
    plan_log_key,
    plan_output_key,
    provider_cache_key,
    state_key,
)
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)


async def artifact_retention_cycle() -> None:
    """Top-level entry point called by the distributed scheduler."""
    from terrapod.db.session import get_db_session
    from terrapod.storage import get_storage

    cfg = settings.artifact_retention
    storage = get_storage()

    start = time.monotonic()
    try:
        from terrapod.api.metrics import RETENTION_DURATION

        categories = [
            ("state_versions", _cleanup_state_versions, cfg.state_versions_keep),
            ("run_artifacts", _cleanup_run_artifacts, cfg.run_artifacts_retention_days),
            ("config_versions", _cleanup_config_versions, cfg.config_versions_retention_days),
            ("provider_cache", _cleanup_provider_cache, cfg.provider_cache_retention_days),
            ("binary_cache", _cleanup_binary_cache, cfg.binary_cache_retention_days),
            ("module_overrides", _cleanup_module_overrides, cfg.module_overrides_retention_days),
            (
                "vcs_archives",
                _cleanup_vcs_archives,
                settings.vcs.archive_cache_retention_days,
            ),
        ]

        for category, handler, threshold in categories:
            if threshold == 0:
                continue
            try:
                async with get_db_session() as db:
                    deleted = await handler(db, storage, threshold, cfg.batch_size)
                    if deleted > 0:
                        logger.info(
                            "Retention cleanup completed",
                            category=category,
                            deleted=deleted,
                        )
            except Exception:
                from terrapod.api.metrics import RETENTION_ERRORS

                RETENTION_ERRORS.labels(category=category).inc()
                logger.warning(
                    "Retention cleanup failed for category",
                    category=category,
                    exc_info=True,
                )

        duration = time.monotonic() - start
        RETENTION_DURATION.observe(duration)
        logger.info("Artifact retention cycle completed", duration_seconds=round(duration, 2))

    except Exception:
        logger.error("Artifact retention cycle failed", exc_info=True)


async def _cleanup_state_versions(
    db: AsyncSession,
    storage: ObjectStore,
    keep: int,
    batch_size: int,
) -> int:
    """Delete excess state versions per workspace, keeping the N newest."""
    from terrapod.api.metrics import RETENTION_DELETED

    deleted = 0

    # Get workspace IDs that have more than `keep` state versions
    count_subq = (
        select(
            StateVersion.workspace_id,
            func.count(StateVersion.id).label("sv_count"),
        )
        .group_by(StateVersion.workspace_id)
        .having(func.count(StateVersion.id) > keep)
        .subquery()
    )

    result = await db.execute(
        select(Workspace.id, Workspace.state_diverged).where(
            Workspace.id == count_subq.c.workspace_id,
        )
    )
    workspaces = result.all()

    for ws_id, state_diverged in workspaces:
        if deleted >= batch_size:
            break

        # Skip workspaces with diverged state — operator may need all versions
        if state_diverged:
            continue

        # Get excess state versions (skip the newest `keep`)
        excess_stmt = (
            select(StateVersion)
            .where(StateVersion.workspace_id == ws_id)
            .order_by(StateVersion.serial.desc())
            .offset(keep)
            .limit(batch_size - deleted)
        )
        excess_result = await db.execute(excess_stmt)
        excess = list(excess_result.scalars().all())

        for sv in excess:
            try:
                await storage.delete(state_key(str(ws_id), str(sv.id)))
            except Exception:
                logger.warning(
                    "Failed to delete state version from storage",
                    workspace_id=str(ws_id),
                    state_version_id=str(sv.id),
                    exc_info=True,
                )
            await db.delete(sv)
            deleted += 1

        await db.flush()

    if deleted:
        await db.commit()
        RETENTION_DELETED.labels(category="state_versions").inc(deleted)

    return deleted


async def _cleanup_run_artifacts(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Delete logs and plan outputs for old terminal runs."""
    from terrapod.api.metrics import RETENTION_DELETED

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    result = await db.execute(
        select(Run)
        .where(
            Run.status.in_(TERMINAL_STATES),
            Run.created_at < cutoff,
        )
        .limit(batch_size)
    )
    runs = list(result.scalars().all())

    for run in runs:
        ws_id = str(run.workspace_id)
        run_id = str(run.id)
        artifact_count = 0

        for key_fn in (plan_log_key, apply_log_key, plan_output_key):
            try:
                await storage.delete(key_fn(ws_id, run_id))
                artifact_count += 1
            except Exception:
                logger.warning(
                    "Failed to delete run artifact from storage",
                    run_id=run_id,
                    key_fn=key_fn.__name__,
                    exc_info=True,
                )

        deleted += artifact_count

    if deleted:
        RETENTION_DELETED.labels(category="run_artifacts").inc(deleted)

    return deleted


async def _cleanup_config_versions(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Delete old config version tarballs not referenced by non-terminal runs."""
    from terrapod.api.metrics import RETENTION_DELETED

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    # Subquery: CV IDs referenced by non-terminal runs
    active_cv_ids = (
        select(Run.configuration_version_id)
        .where(
            Run.configuration_version_id.isnot(None),
            Run.status.notin_(TERMINAL_STATES),
        )
        .distinct()
        .scalar_subquery()
    )

    result = await db.execute(
        select(ConfigurationVersion)
        .where(
            ConfigurationVersion.created_at < cutoff,
            ConfigurationVersion.id.notin_(active_cv_ids),
        )
        .limit(batch_size)
    )
    cvs = list(result.scalars().all())

    for cv in cvs:
        try:
            await storage.delete(config_version_key(str(cv.workspace_id), str(cv.id)))
        except Exception:
            logger.warning(
                "Failed to delete config version from storage",
                config_version_id=str(cv.id),
                exc_info=True,
            )
        await db.delete(cv)
        deleted += 1

    if deleted:
        await db.commit()
        RETENTION_DELETED.labels(category="config_versions").inc(deleted)

    return deleted


async def _cleanup_provider_cache(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Delete provider cache entries not accessed within retention_days."""
    from terrapod.api.metrics import RETENTION_DELETED

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    result = await db.execute(
        select(CachedProviderPackage)
        .where(CachedProviderPackage.last_accessed_at < cutoff)
        .limit(batch_size)
    )
    entries = list(result.scalars().all())

    for entry in entries:
        try:
            key = provider_cache_key(
                entry.hostname,
                entry.namespace,
                entry.type,
                entry.version,
                entry.filename,
            )
            await storage.delete(key)
        except Exception:
            logger.warning(
                "Failed to delete provider cache entry from storage",
                entry_id=str(entry.id),
                exc_info=True,
            )
        await db.delete(entry)
        deleted += 1

    if deleted:
        await db.commit()
        RETENTION_DELETED.labels(category="provider_cache").inc(deleted)

    return deleted


async def _cleanup_binary_cache(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Delete binary cache entries not accessed within retention_days."""
    from terrapod.api.metrics import RETENTION_DELETED

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    result = await db.execute(
        select(CachedBinary).where(CachedBinary.last_accessed_at < cutoff).limit(batch_size)
    )
    entries = list(result.scalars().all())

    for entry in entries:
        try:
            key = binary_cache_key(entry.tool, entry.version, entry.os, entry.arch)
            await storage.delete(key)
        except Exception:
            logger.warning(
                "Failed to delete binary cache entry from storage",
                entry_id=str(entry.id),
                exc_info=True,
            )
        await db.delete(entry)
        deleted += 1

    if deleted:
        await db.commit()
        RETENTION_DELETED.labels(category="binary_cache").inc(deleted)

    return deleted


async def _cleanup_module_overrides(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Delete module override tarballs for old terminal runs."""
    from terrapod.api.metrics import RETENTION_DELETED

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    result = await db.execute(
        select(Run)
        .where(
            Run.status.in_(TERMINAL_STATES),
            Run.module_overrides.isnot(None),
            Run.created_at < cutoff,
        )
        .limit(batch_size)
    )
    runs = list(result.scalars().all())

    for run in runs:
        overrides = run.module_overrides or {}
        for _coord, storage_path in overrides.items():
            try:
                await storage.delete(storage_path)
            except Exception:
                logger.warning(
                    "Failed to delete module override from storage",
                    run_id=str(run.id),
                    path=storage_path,
                    exc_info=True,
                )
            deleted += 1

        run.module_overrides = None

    if deleted:
        await db.commit()
        RETENTION_DELETED.labels(category="module_overrides").inc(deleted)

    return deleted


async def _cleanup_vcs_archives(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Delete cached VCS archive tarballs older than retention_days.

    VCS archives are cached in object storage at
    ``vcs_archives/{conn_id}/{owner}/{repo}/{sha}.tar.gz``. They have no DB
    table — entries are content-addressed by commit SHA, so we list the prefix
    and use each object's `last_modified` timestamp directly. No `db.commit()`
    is needed because no relational state changes.

    `db` is unused here but kept on the signature to match the other handlers.

    Scaling: `list_prefix` returns ALL entries under the prefix in one call.
    For typical fleets (≤100 monorepos × ≤10 unique SHAs per workspace per
    week) this is well under 10k keys. If a deployment grows past that, the
    handler logs a warning so operators can move to a DB-tracked index;
    until then this single-call approach keeps the contract simple.
    """
    from terrapod.api.metrics import RETENTION_DELETED

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0
    _LIST_WARN_THRESHOLD = 10_000

    try:
        entries = await storage.list_prefix("vcs_archives/")
    except Exception:
        logger.warning("Failed to list vcs_archives prefix", exc_info=True)
        return 0

    if len(entries) >= _LIST_WARN_THRESHOLD:
        logger.warning(
            "vcs_archives prefix has many entries; consider DB-tracked index",
            entry_count=len(entries),
            threshold=_LIST_WARN_THRESHOLD,
        )

    # Oldest first so we evict the longest-stale entries first if we hit the batch cap.
    entries.sort(key=lambda m: m.last_modified)
    for meta in entries:
        if meta.last_modified >= cutoff:
            # Sorted oldest first; once we see anything still in retention,
            # everything after is also in retention.
            break
        if deleted >= batch_size:
            break
        try:
            await storage.delete(meta.key)
            deleted += 1
        except Exception:
            logger.warning(
                "Failed to delete VCS archive from storage",
                key=meta.key,
                exc_info=True,
            )

    if deleted:
        RETENTION_DELETED.labels(category="vcs_archives").inc(deleted)

    return deleted
