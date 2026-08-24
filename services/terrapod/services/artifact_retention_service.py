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
import uuid
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
    plan_json_output_key,
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
            (
                "config_versions_count",
                _cleanup_config_versions_count,
                cfg.config_versions_keep,
            ),
            ("provider_cache", _cleanup_provider_cache, cfg.provider_cache_retention_days),
            ("binary_cache", _cleanup_binary_cache, cfg.binary_cache_retention_days),
            ("package_cache", _cleanup_package_cache, cfg.package_cache_retention_days),
            ("module_overrides", _cleanup_module_overrides, cfg.module_overrides_retention_days),
            (
                "vcs_archives",
                _cleanup_vcs_archives,
                settings.vcs.archive_cache_retention_days,
            ),
            (
                "deleted_workspaces",
                _cleanup_deleted_workspaces,
                cfg.deleted_workspace_retention_days,
            ),
        ]

        # Sealed (cache-only) mode: never evict the binary/provider caches — an
        # un-refetchable artifact would be lost permanently. Other categories
        # (state/run/config artifacts) are unaffected.
        sealed_skip = (
            {"provider_cache", "binary_cache", "package_cache"}
            if settings.registry.cache_only
            else set()
        )

        for category, handler, threshold in categories:
            if threshold == 0:
                continue
            if category in sealed_skip:
                logger.info("Skipping cache eviction in sealed mode", category=category)
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

    flag_resets = 0
    for run in runs:
        ws_id = str(run.workspace_id)
        run_id = str(run.id)
        artifact_count = 0

        for key_fn in (plan_log_key, apply_log_key, plan_output_key, plan_json_output_key):
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

        # Keep the row's `has_json_output` flag honest: if the artifact
        # is gone, the plan-show response must stop advertising a URL
        # that would 404.
        if run.has_json_output:
            run.has_json_output = False
            flag_resets += 1

        deleted += artifact_count

    if flag_resets:
        await db.commit()

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


async def _cleanup_config_versions_count(
    db: AsyncSession,
    storage: ObjectStore,
    keep: int,
    batch_size: int,
) -> int:
    """Delete excess configuration versions per workspace, keeping the N newest.

    Mirrors `_cleanup_state_versions`'s pattern: workspaces with more
    than `keep` CVs get the oldest pruned. CVs referenced by a non-
    terminal run are excluded (same safeguard as the TTL-based
    cleanup) so an in-flight run doesn't lose its source bytes
    mid-execution.

    The CV row itself stays — the FK from `runs.configuration_version_id`
    is `ON DELETE SET NULL`, but a downstream run referring back to
    its CV is still valuable history. Only the tarball goes; the
    historical record persists.

    Wait — that's wrong. We DO delete the row here, matching the
    existing TTL cleanup. The Run.configuration_version_id field is
    SET NULL on cascade, so any referencing run keeps its row but
    loses the pointer. That's the documented trade-off for retention.
    """
    from terrapod.api.metrics import RETENTION_DELETED

    deleted = 0

    # Workspaces with > keep CVs. We can't restrict this query to
    # exclude active-run-referenced CVs at the SQL level cleanly —
    # the safety filter happens row-by-row below.
    count_subq = (
        select(
            ConfigurationVersion.workspace_id,
            func.count(ConfigurationVersion.id).label("cv_count"),
        )
        .group_by(ConfigurationVersion.workspace_id)
        .having(func.count(ConfigurationVersion.id) > keep)
        .subquery()
    )

    result = await db.execute(select(Workspace.id).where(Workspace.id == count_subq.c.workspace_id))
    workspace_ids = [row[0] for row in result.all()]

    # CVs we must NOT touch — currently in-flight on a non-terminal run.
    active_cv_ids = (
        select(Run.configuration_version_id)
        .where(
            Run.configuration_version_id.isnot(None),
            Run.status.notin_(TERMINAL_STATES),
        )
        .distinct()
        .scalar_subquery()
    )

    for ws_id in workspace_ids:
        if deleted >= batch_size:
            break

        # Excess CVs for this workspace, ordered by created_at DESC,
        # skipping the newest `keep`. Filter out anything an active
        # run still needs.
        excess_stmt = (
            select(ConfigurationVersion)
            .where(
                ConfigurationVersion.workspace_id == ws_id,
                ConfigurationVersion.id.notin_(active_cv_ids),
            )
            .order_by(ConfigurationVersion.created_at.desc())
            .offset(keep)
            .limit(batch_size - deleted)
        )
        excess_result = await db.execute(excess_stmt)
        excess = list(excess_result.scalars().all())

        for cv in excess:
            try:
                await storage.delete(config_version_key(str(cv.workspace_id), str(cv.id)))
            except Exception:
                logger.warning(
                    "Failed to delete config version tarball from storage",
                    config_version_id=str(cv.id),
                    exc_info=True,
                )
            await db.delete(cv)
            deleted += 1

        await db.flush()

    if deleted:
        await db.commit()
        RETENTION_DELETED.labels(category="config_versions_count").inc(deleted)

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


async def _cleanup_package_cache(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Delete cached PyPI/npm artifacts not accessed within retention_days.

    Access-based, like the other pull-through caches: a package every run
    installs would otherwise be evicted for being old and re-fetched immediately,
    which is worse than not expiring it at all.
    """
    from terrapod.api.metrics import RETENTION_DELETED
    from terrapod.db.models import CachedPackageFile

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    deleted = 0

    result = await db.execute(
        select(CachedPackageFile)
        .where(CachedPackageFile.last_accessed_at < cutoff)
        .limit(batch_size)
    )
    entries = list(result.scalars().all())

    for entry in entries:
        try:
            await storage.delete(entry.storage_key)
        except Exception:
            # An object already gone is the row's problem, not a reason to keep
            # the row: leaving it would make every later request a miss that
            # re-fetches and then collides on the unique constraint.
            logger.warning(
                "Failed to delete cached package artifact from storage",
                entry_id=str(entry.id),
                exc_info=True,
            )
        await db.delete(entry)
        deleted += 1

    if deleted:
        await db.commit()
        RETENTION_DELETED.labels(category="package_cache").inc(deleted)

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


#: Below this many orphans, the plausibility check never fires. A small
#: deployment legitimately has more deleted workspaces than live ones, and a
#: brand-new one has none live at all — a pure ratio would refuse to reap
#: exactly where there is nothing to protect.
_ORPHAN_ALARM_FLOOR = 25

#: Above this fraction of the live workspace count, an orphan set stops looking
#: like deletions and starts looking like a database that has lost rows.
_ORPHAN_ALARM_RATIO = 0.5


async def _orphan_set_is_implausible(db: AsyncSession, orphan_count: int) -> bool:
    """Refuse to reap when the orphan set is too large to be real deletions.

    This category inverts the safety property of every other one. The others
    walk DB rows and delete the blobs those rows reference, so what is deleted
    depends on the database being **correct**. This one lists blobs and deletes
    whatever the database does not claim — so what is deleted depends on the
    database being **complete**.

    That difference matters because a database can be incomplete without being
    wrong. Restore Postgres from a backup taken before a batch of workspaces
    was created, or repoint `DATABASE_URL` at the wrong instance during a
    migration, and every one of those workspaces reads as an orphan: stamped on
    the first cycle, then permanently deleted a retention window later. The
    existing `still_referenced` re-check cannot catch it, because missing rows
    are precisely what a stale database has.

    Nothing available here can tell "the DB lost rows" from "someone deleted a
    lot of workspaces". What it can do is notice that the ratio is implausible
    and decline to act — reclaiming storage is never urgent, and the failure
    modes are not symmetric: a delayed reap costs disk, a wrong one costs the
    only remaining copy of a customer's state.
    """
    # Imported here, like every other metric in this module: `api.metrics`
    # pulls in the app, and this service is imported from it.
    from terrapod.api.metrics import RETENTION_ORPHAN_REAP_BLOCKED

    if orphan_count <= _ORPHAN_ALARM_FLOOR:
        RETENTION_ORPHAN_REAP_BLOCKED.set(0)
        return False

    live_total = (
        await db.execute(select(func.count()).select_from(Workspace))
    ).scalar_one_or_none() or 0
    if orphan_count <= live_total * _ORPHAN_ALARM_RATIO:
        RETENTION_ORPHAN_REAP_BLOCKED.set(0)
        return False

    RETENTION_ORPHAN_REAP_BLOCKED.set(1)
    logger.error(
        "Refusing to reap orphaned workspace state — the orphan set is implausibly "
        "large for the number of live workspaces, which is what a database restored "
        "from an older backup or a repointed DATABASE_URL looks like. No state has "
        "been deleted. Confirm the database is the right one and complete; reaping "
        "resumes on its own once the ratio is plausible.",
        orphans=orphan_count,
        live_workspaces=live_total,
        ratio_threshold=_ORPHAN_ALARM_RATIO,
        floor=_ORPHAN_ALARM_FLOOR,
    )
    return True


async def _cleanup_deleted_workspaces(
    db: AsyncSession,
    storage: ObjectStore,
    retention_days: int,
    batch_size: int,
) -> int:
    """Reap state left behind by deleted workspaces (#1253).

    Every other category here walks DB rows and deletes the blobs they point
    at. This one cannot: deleting a workspace CASCADEs its `StateVersion` rows
    away, so by the time the state is garbage there is no row left to find it
    from. Its blobs become permanently invisible to the row-driven reaper —
    which is why, before this existed, every workspace ever deleted still
    occupied storage forever.

    So the orphan set is computed the only way still available: list the state
    prefixes present in storage, subtract the workspaces that still exist.

    **Discovery stamps; it never deletes.** An orphan with no marker gets one
    written dated now, and is left alone until a later cycle. That is what
    makes the retention window mean something: nothing is reaped that has not
    been *visible as recoverable* for the full window. Dating from the newest
    blob instead would expire a workspace that was last applied months before
    someone deleted it, giving no undelete window at all — and a workspace
    deleted before this feature shipped would be reaped on the first cycle.
    """
    from terrapod.services import deleted_workspace_service as dws
    from terrapod.storage.keys import DELETED_MARKER_PREFIX

    # Prefixes look like `state/{workspace_id}/{version}.tfstate`. The marker
    # prefix and the DR index live under `state/` too and are not workspaces.
    seen: set[str] = set()
    for obj in await storage.list_prefix("state/"):
        key = obj.key
        if key.startswith(DELETED_MARKER_PREFIX) or "/" not in key[len("state/") :]:
            continue
        candidate = key[len("state/") :].split("/", 1)[0]
        # `Workspace.id` is a UUID column, so a stray non-UUID directory would
        # raise on bind and — swallowed by this cycle's per-category except —
        # silently kill the reaper for good. Skip anything unparseable rather
        # than letting one junk prefix disable reaping for every workspace.
        try:
            uuid.UUID(candidate)
        except ValueError:
            logger.warning("Ignoring non-UUID prefix under state/", prefix=candidate)
            continue
        seen.add(candidate)

    if not seen:
        return 0

    live = {
        str(w)
        for w in (await db.execute(select(Workspace.id).where(Workspace.id.in_(seen)))).scalars()
    }
    orphans = sorted(seen - live)

    if await _orphan_set_is_implausible(db, len(orphans)):
        return 0

    now = datetime.now(UTC)
    reaped = 0
    for ws_id in orphans[:batch_size]:
        marker = await dws.read_marker(storage, ws_id)

        if marker is None:
            # First sight (or an unreadable marker): start the clock, delete
            # nothing this cycle.
            await dws.write_marker(storage, ws_id, dws.build_discovery_marker(ws_id, now))
            logger.info("Stamped orphaned workspace state for retention", workspace_id=ws_id)
            continue

        age = dws.marker_age_days(marker, now)
        if age is None:
            # A marker we cannot date is treated as unstamped rather than as
            # expired — re-stamping restarts the window instead of reaping now.
            await dws.write_marker(storage, ws_id, dws.build_discovery_marker(ws_id, now))
            continue
        if age < retention_days:
            continue

        # Belt and braces against a listing/DB skew: never reap a prefix that
        # still has a live state-version row.
        still_referenced = (
            await db.execute(
                select(StateVersion.id).where(StateVersion.workspace_id == ws_id).limit(1)
            )
        ).scalar_one_or_none()
        if still_referenced is not None:
            logger.warning(
                "Orphan still has state-version rows — not reaping",
                workspace_id=ws_id,
            )
            continue

        for obj in await storage.list_prefix(f"state/{ws_id}/"):
            try:
                await storage.delete(obj.key)
            except Exception as e:  # noqa: BLE001 — best-effort, matches this module
                logger.warning("Failed to delete orphaned state object", key=obj.key, error=str(e))
        try:
            await storage.delete(dws.deleted_workspace_marker_key(ws_id))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to delete marker", workspace_id=ws_id, error=str(e))

        logger.info(
            "Reaped state for deleted workspace past its retention window",
            workspace_id=ws_id,
            workspace_name=marker.get("workspace_name"),
            age_days=round(age, 1),
        )
        reaped += 1

    if reaped:
        # Every other retention category counts what it removed; this one did
        # not, which left the ONLY category that irreversibly destroys customer
        # state as the only one an operator could not graph, alert on, or
        # reconcile against afterwards (#1299). A deletion nobody can see
        # happen is indistinguishable from one that never happened until
        # somebody goes looking for the data.
        from terrapod.api.metrics import RETENTION_DELETED

        RETENTION_DELETED.labels(category="deleted_workspaces").inc(reaped)

    return reaped
