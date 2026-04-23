"""VCS poller — background task that detects new commits and triggers runs.

Polling-first design: Terrapod polls VCS providers for changes (outbound
HTTPS only). When webhooks are configured, they enqueue a triggered task
via the distributed scheduler for cross-replica immediate polling.

Each workspace tracks a branch (e.g. main). Pushes to that branch create
real plan/apply runs. Open PRs/MRs targeting that branch create speculative
(plan-only) runs.

Provider-agnostic: dispatches to GitHub or GitLab based on the VCS connection's
provider field.

Scheduling: The poll cycle is registered as a periodic task with the
distributed scheduler. In a multi-replica deployment, exactly one replica
runs each poll cycle per interval. Webhook-triggered immediate polls use
the scheduler's trigger queue with deduplication.
"""

import asyncio
import time as time_mod
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.metrics import (
    VCS_COMMITS_DETECTED,
    VCS_POLL_DURATION,
    VCS_PRS_DETECTED,
    VCS_RUNS_CREATED,
)
from terrapod.db.models import Run, VCSConnection, Workspace, utc_now
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, run_service
from terrapod.services.archive_utils import strip_archive_top_level_dir_async
from terrapod.services.vcs_provider import PullRequest
from terrapod.storage import get_storage
from terrapod.storage.keys import config_version_key

logger = get_logger(__name__)


# --- Provider dispatch ---


def _parse_repo_url(conn: VCSConnection, repo_url: str) -> tuple[str, str] | None:
    """Parse a repo URL using the appropriate provider parser."""
    if conn.provider == "gitlab":
        return gitlab_service.parse_repo_url(repo_url)
    return github_service.parse_repo_url(repo_url)


async def _get_branch_sha(conn: VCSConnection, owner: str, repo: str, branch: str) -> str | None:
    """Get branch HEAD SHA via the appropriate provider."""
    if conn.provider == "gitlab":
        return await gitlab_service.get_branch_sha(conn, owner, repo, branch)
    return await github_service.get_repo_branch_sha(conn, owner, repo, branch)


async def _get_default_branch(conn: VCSConnection, owner: str, repo: str) -> str | None:
    """Get default branch via the appropriate provider."""
    if conn.provider == "gitlab":
        return await gitlab_service.get_default_branch(conn, owner, repo)
    return await github_service.get_repo_default_branch(conn, owner, repo)


async def _download_archive(conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
    """Download archive via the appropriate provider."""
    if conn.provider == "gitlab":
        return await gitlab_service.download_archive(conn, owner, repo, ref)
    return await github_service.download_repo_archive(conn, owner, repo, ref)


async def _get_changed_files(
    conn: VCSConnection, owner: str, repo: str, base_sha: str, head_sha: str
) -> list[str] | None:
    """Get changed files between two commits via the appropriate provider.

    Returns None if the response is truncated (too many files), signaling
    the caller should skip filtering and create the run unconditionally.
    """
    if conn.provider == "gitlab":
        return await gitlab_service.get_changed_files(conn, owner, repo, base_sha, head_sha)
    return await github_service.get_changed_files(conn, owner, repo, base_sha, head_sha)


def _changes_affect_prefixes(changed_files: list[str], prefixes: list[str]) -> bool:
    """Check if any changed files fall within any of the given directory prefixes.

    Uses strict prefix matching: only files starting with "{prefix}/" are
    considered relevant. Root-level files don't trigger subdirectory workspaces.
    """
    if not prefixes:
        return False
    normalized = [p.rstrip("/") + "/" for p in prefixes]
    return any(f.startswith(n) for f in changed_files for n in normalized)


async def _list_branches(conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
    """List branches via the appropriate provider."""
    if conn.provider == "gitlab":
        return await gitlab_service.list_branches(conn, owner, repo)
    return await github_service.list_repo_branches(conn, owner, repo)


async def _list_tags(conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
    """List tags via the appropriate provider."""
    if conn.provider == "gitlab":
        return await gitlab_service.list_tags(conn, owner, repo)
    return await github_service.list_repo_tags(conn, owner, repo)


async def _list_open_prs(
    conn: VCSConnection, owner: str, repo: str, base_branch: str
) -> list[PullRequest]:
    """List open PRs/MRs via the appropriate provider."""
    if conn.provider == "gitlab":
        return await gitlab_service.list_open_prs(conn, owner, repo, base_branch)
    prs = await github_service.list_open_pull_requests(conn, owner, repo, base_branch)
    return [
        PullRequest(
            number=pr["number"],
            head_sha=pr["head_sha"],
            head_ref=pr["head_ref"],
            title=pr["title"],
        )
        for pr in prs
    ]


# --- Shared logic ---


async def _resolve_branch(conn: VCSConnection, ws: Workspace, owner: str, repo: str) -> str | None:
    """Resolve the tracked branch for a workspace."""
    if ws.vcs_branch:
        return ws.vcs_branch

    try:
        default_branch = await _get_default_branch(conn, owner, repo)
        if default_branch:
            return default_branch
        logger.warning(
            "Cannot determine default branch",
            workspace=ws.name,
            repo=f"{owner}/{repo}",
        )
    except Exception as e:
        logger.error(
            "Failed to get default branch",
            workspace=ws.name,
            repo=f"{owner}/{repo}",
            error=str(e),
        )
    return None


_strip_top_level_dir = strip_archive_top_level_dir_async  # alias for internal use


async def _create_vcs_run(
    db: AsyncSession,
    ws: Workspace,
    conn: VCSConnection,
    owner: str,
    repo: str,
    sha: str,
    branch: str,
    *,
    speculative: bool = False,
    pr_number: int | None = None,
    message: str = "",
) -> Run | None:
    """Download archive, create ConfigurationVersion and Run.

    Defensive dedup: if a run already exists for this (workspace, sha, branch,
    pr_number), return None rather than creating a duplicate. This catches
    races from any path that might create VCS-sourced runs — including the
    vcs_poll ↔ vcs_immediate_poll race fixed by the CAS in _poll_workspace_branch.
    """
    # Belt-and-braces against any path creating a duplicate run for the same
    # commit — the CAS in _poll_workspace_branch is the primary race closer.
    dedup_q = select(Run).where(
        Run.workspace_id == ws.id,
        Run.vcs_commit_sha == sha,
        Run.vcs_branch == branch,
    )
    if pr_number is None:
        dedup_q = dedup_q.where(Run.vcs_pull_request_number.is_(None))
    else:
        dedup_q = dedup_q.where(Run.vcs_pull_request_number == pr_number)
    existing = await db.execute(dedup_q.limit(1))
    if existing.scalar_one_or_none() is not None:
        logger.info(
            "Skipping run creation — duplicate run already exists for this commit",
            workspace=ws.name,
            sha=sha[:8],
            branch=branch,
            pr_number=pr_number,
        )
        return None

    try:
        archive = await _download_archive(conn, owner, repo, sha)
    except Exception as e:
        logger.error(
            "Failed to download repo archive",
            workspace=ws.name,
            ref=sha[:8],
            error=str(e),
        )
        return None

    # VCS tarballs have a top-level directory wrapper — strip it
    archive = await _strip_top_level_dir(archive)

    cv = await run_service.create_configuration_version(
        db,
        workspace_id=ws.id,
        source="vcs",
        auto_queue_runs=False,
        speculative=speculative,
    )
    await db.flush()

    storage = get_storage()
    key = config_version_key(str(ws.id), str(cv.id))
    await storage.put(key, archive, content_type="application/x-tar")

    cv = await run_service.mark_configuration_uploaded(db, cv)

    run = await run_service.create_run(
        db,
        workspace=ws,
        message=message,
        source="vcs",
        plan_only=speculative,
        configuration_version_id=cv.id,
        created_by="vcs-poller",
    )

    run.vcs_commit_sha = sha
    run.vcs_branch = branch
    if pr_number is not None:
        run.vcs_pull_request_number = pr_number

    run = await run_service.queue_run(db, run)
    return run


async def _poll_workspace_branch(
    db: AsyncSession,
    ws: Workspace,
    conn: VCSConnection,
    owner: str,
    repo: str,
    branch: str,
) -> None:
    """Check the tracked branch for new commits and create a run."""
    sha = await _get_branch_sha(conn, owner, repo, branch)

    if sha is None:
        logger.warning(
            "Branch not found",
            workspace=ws.name,
            repo=f"{owner}/{repo}",
            branch=branch,
        )
        return

    if sha == ws.vcs_last_commit_sha:
        return

    # Atomic compare-and-set: advance vcs_last_commit_sha to the new sha ONLY
    # if no concurrent poll cycle has already done so. Closes the race between
    # periodic vcs_poll and webhook-triggered vcs_immediate_poll (issue #217).
    # If the CAS affects zero rows, another poll already handled this commit —
    # we bail without creating a run.
    old_sha = ws.vcs_last_commit_sha
    cas = (
        update(Workspace)
        .where(Workspace.id == ws.id)
        .where(Workspace.vcs_last_commit_sha.is_not_distinct_from(old_sha))
        .values(vcs_last_commit_sha=sha)
        .returning(Workspace.id)
    )
    cas_result = await db.execute(cas)
    if cas_result.scalar_one_or_none() is None:
        logger.debug(
            "Concurrent VCS poll already advanced this workspace, bailing",
            workspace=ws.name,
            sha=sha[:8],
        )
        return
    # Keep the in-memory ORM state consistent with the DB.
    ws.vcs_last_commit_sha = sha
    await db.commit()

    VCS_COMMITS_DETECTED.labels(provider=conn.provider).inc()

    logger.info(
        "New commit detected",
        workspace=ws.name,
        repo=f"{owner}/{repo}",
        branch=branch,
        old_sha=old_sha[:8] if old_sha else "(none)",
        new_sha=sha[:8],
    )

    # VCS subdirectory filtering: skip runs when changes don't affect the workspace
    effective_prefixes = (
        ws.trigger_prefixes
        if ws.trigger_prefixes
        else ([ws.working_directory] if ws.working_directory else [])
    )
    if effective_prefixes and old_sha:
        try:
            changed = await _get_changed_files(conn, owner, repo, old_sha, sha)
            # None means truncated — create run unconditionally
            if changed is not None and not _changes_affect_prefixes(changed, effective_prefixes):
                logger.info(
                    "Skipping run — no changes match trigger prefixes",
                    workspace=ws.name,
                    trigger_prefixes=effective_prefixes,
                    changed_files=changed[:20],
                    changed_files_count=len(changed),
                )
                return
        except Exception as e:
            logger.warning(
                "Failed to get changed files, creating run anyway",
                workspace=ws.name,
                error=repr(e),
            )

    run = await _create_vcs_run(
        db,
        ws,
        conn,
        owner,
        repo,
        sha,
        branch,
        message=f"Triggered by commit {sha[:8]} on {branch}",
    )

    if run:
        VCS_RUNS_CREATED.labels(provider=conn.provider, type="push").inc()

        logger.info(
            "VCS run created",
            workspace=ws.name,
            run_id=str(run.id),
            commit_sha=sha[:8],
            branch=branch,
        )


async def _poll_workspace_prs(
    db: AsyncSession,
    ws: Workspace,
    conn: VCSConnection,
    owner: str,
    repo: str,
    branch: str,
) -> None:
    """Check open PRs/MRs targeting the tracked branch for speculative plans."""
    prs = await _list_open_prs(conn, owner, repo, branch)

    for pr in prs:
        # Check if we already have any run for this PR + SHA (avoid duplicates)
        existing = await db.execute(
            select(Run)
            .where(
                Run.workspace_id == ws.id,
                Run.vcs_pull_request_number == pr.number,
                Run.vcs_commit_sha == pr.head_sha,
            )
            .limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            continue

        # Cancel any existing non-terminal runs for this PR (superseded by new commit)
        stale_result = await db.execute(
            select(Run).where(
                Run.workspace_id == ws.id,
                Run.vcs_pull_request_number == pr.number,
                Run.status.notin_(run_service.TERMINAL_STATES),
            )
        )
        for stale_run in stale_result.scalars().all():
            try:
                await run_service.cancel_run(db, stale_run)
                logger.info(
                    "Canceled stale PR run",
                    run_id=str(stale_run.id),
                    pr_number=pr.number,
                    workspace=ws.name,
                )
            except Exception as e:
                logger.warning(
                    "Failed to cancel stale PR run",
                    run_id=str(stale_run.id),
                    error=str(e),
                )

        VCS_PRS_DETECTED.labels(provider=conn.provider).inc()

        logger.info(
            "New PR commit detected",
            workspace=ws.name,
            pr_number=pr.number,
            head_ref=pr.head_ref,
            head_sha=pr.head_sha[:8],
            title=pr.title,
        )

        # VCS subdirectory filtering for PRs: compare PR head against tracked branch
        pr_prefixes = (
            ws.trigger_prefixes
            if ws.trigger_prefixes
            else ([ws.working_directory] if ws.working_directory else [])
        )
        if pr_prefixes and ws.vcs_last_commit_sha:
            try:
                changed = await _get_changed_files(
                    conn, owner, repo, ws.vcs_last_commit_sha, pr.head_sha
                )
                # None means truncated — create run unconditionally
                if changed is not None and not _changes_affect_prefixes(changed, pr_prefixes):
                    logger.info(
                        "Skipping PR run — no changes match trigger prefixes",
                        workspace=ws.name,
                        pr_number=pr.number,
                        trigger_prefixes=pr_prefixes,
                        changed_files=changed[:20],
                        changed_files_count=len(changed),
                    )
                    continue
            except Exception as e:
                logger.warning(
                    "Failed to get changed files for PR, creating run anyway",
                    workspace=ws.name,
                    pr_number=pr.number,
                    error=repr(e),
                )

        run = await _create_vcs_run(
            db,
            ws,
            conn,
            owner,
            repo,
            pr.head_sha,
            pr.head_ref,
            speculative=True,
            pr_number=pr.number,
            message=f"Speculative plan for PR #{pr.number}: {pr.title}",
        )

        if run:
            VCS_RUNS_CREATED.labels(provider=conn.provider, type="pr").inc()
            await db.commit()
            logger.info(
                "Speculative run created for PR",
                workspace=ws.name,
                run_id=str(run.id),
                pr_number=pr.number,
                head_sha=pr.head_sha[:8],
            )


async def _poll_workspace(db: AsyncSession, ws: Workspace) -> None:
    """Poll a single workspace: check branch for pushes and PRs for speculative plans."""
    if not ws.vcs_repo_url or not ws.vcs_connection_id:
        return

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if not conn or conn.status != "active":
        ws.vcs_last_error = "VCS connection is not active"
        ws.vcs_last_error_at = utc_now()
        logger.warning(
            "VCS connection not active",
            workspace=ws.name,
            connection_id=str(ws.vcs_connection_id),
        )
        return

    parsed = _parse_repo_url(conn, ws.vcs_repo_url)
    if not parsed:
        ws.vcs_last_error = f"Cannot parse VCS repo URL: {ws.vcs_repo_url}"
        ws.vcs_last_error_at = utc_now()
        logger.warning(
            "Cannot parse VCS repo URL",
            workspace=ws.name,
            url=ws.vcs_repo_url,
            provider=conn.provider,
        )
        return

    owner, repo = parsed

    branch = await _resolve_branch(conn, ws, owner, repo)
    if not branch:
        ws.vcs_last_error = "Cannot determine tracked branch"
        ws.vcs_last_error_at = utc_now()
        return

    try:
        # 1. Check tracked branch for new commits → real runs
        await _poll_workspace_branch(db, ws, conn, owner, repo, branch)

        # 2. Check open PRs/MRs targeting the tracked branch → speculative plans
        await _poll_workspace_prs(db, ws, conn, owner, repo, branch)

        # Success: update last-polled timestamp and clear any previous error
        ws.vcs_last_polled_at = utc_now()
        ws.vcs_last_error = None
        ws.vcs_last_error_at = None
    except Exception as e:
        logger.error(
            "Error polling workspace VCS",
            workspace=ws.name,
            error=str(e),
            exc_info=e,
        )
        ws.vcs_last_error = str(e)[:500]
        ws.vcs_last_error_at = utc_now()


# Bound the number of workspaces polled in parallel per cycle. Each workspace
# makes a handful of GitHub/GitLab API calls (branch SHA, list PRs, changed-files
# per PR); we cap concurrency so one cycle can't exhaust the provider's API
# rate limit or saturate the event loop with outstanding HTTP connections.
# Most deployments have <10 VCS workspaces per repo, so 10 covers the common
# case without bursting.
_MAX_PARALLEL_WORKSPACE_POLLS = 10


async def _poll_workspace_owned(ws_id: uuid.UUID, semaphore: asyncio.Semaphore) -> None:
    """Poll a single workspace in its own DB session, bounded by a semaphore.

    Each parallel poll needs its own session — AsyncSession is not safe to
    share across concurrent coroutines. Errors are caught and logged here
    so one workspace's failure doesn't sink the whole cycle.
    """
    async with semaphore, get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        if ws is None:
            return
        try:
            await _poll_workspace(db, ws)
            await db.commit()
        except Exception as e:
            logger.error(
                "Error polling workspace",
                workspace=ws.name,
                error=str(e),
                exc_info=e,
            )
            try:
                await db.rollback()
            except Exception:
                pass


async def _select_workspace_ids(db: AsyncSession, repo: str | None = None) -> list[uuid.UUID]:
    """Fetch IDs of VCS-enabled workspaces, optionally filtered to one repo.

    The repo filter is used by webhook-triggered immediate polls to avoid
    re-polling every workspace when only one repo had a push. Matches URLs
    like https://github.com/{owner}/{repo}, with or without trailing .git.
    """
    stmt = select(Workspace.id).where(
        Workspace.vcs_repo_url != "",
        Workspace.vcs_connection_id.isnot(None),
    )
    if repo:
        # Match as a suffix rather than exact so http/https, trailing slash,
        # and .git variants all hit.
        like_core = f"%{repo}"
        stmt = stmt.where(
            or_(
                Workspace.vcs_repo_url.like(like_core),
                Workspace.vcs_repo_url.like(f"{like_core}/"),
                Workspace.vcs_repo_url.like(f"{like_core}.git"),
            )
        )
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def poll_cycle() -> None:
    """Execute one poll cycle: check all VCS-enabled workspaces in parallel.

    Called by the distributed scheduler as a periodic task. Only one
    replica runs this per interval across the entire deployment.
    """
    start = time_mod.monotonic()
    async with get_db_session() as db:
        workspace_ids = await _select_workspace_ids(db)

    if not workspace_ids:
        VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)
        return

    logger.debug("VCS poll cycle", workspace_count=len(workspace_ids))

    semaphore = asyncio.Semaphore(_MAX_PARALLEL_WORKSPACE_POLLS)
    await asyncio.gather(
        *[_poll_workspace_owned(wid, semaphore) for wid in workspace_ids],
        return_exceptions=True,
    )

    VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)


async def handle_immediate_poll(payload: dict) -> None:
    """Handle a webhook-triggered immediate poll for a specific repo.

    Called by the distributed scheduler's trigger consumer. The payload
    contains {"repo": "owner/repo"} from the webhook handler. We poll
    only the workspaces that track this repo — no point re-checking every
    other repo's workspaces just because one got a push.
    """
    start = time_mod.monotonic()
    repo = payload.get("repo", "")
    logger.info("Immediate poll triggered by webhook", repo=repo)

    async with get_db_session() as db:
        workspace_ids = await _select_workspace_ids(db, repo=repo or None)

    if not workspace_ids:
        logger.info("Immediate poll: no workspaces match repo", repo=repo)
        VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)
        return

    logger.info(
        "Immediate poll: polling matching workspaces",
        repo=repo,
        workspace_count=len(workspace_ids),
    )

    semaphore = asyncio.Semaphore(_MAX_PARALLEL_WORKSPACE_POLLS)
    await asyncio.gather(
        *[_poll_workspace_owned(wid, semaphore) for wid in workspace_ids],
        return_exceptions=True,
    )

    VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)
