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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.metrics import (
    VCS_COMMITS_DETECTED,
    VCS_POLL_DURATION,
    VCS_PRS_DETECTED,
    VCS_RUNS_CREATED,
)
from terrapod.db.models import (
    AutodiscoveryRule,
    PRSession,
    Run,
    VCSConnection,
    Workspace,
    now_utc,
)
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, run_service
from terrapod.services.scheduler import enqueue_trigger
from terrapod.services.vcs_archive_cache import VCSArchiveCache, materialize_archive
from terrapod.services.vcs_provider import PullRequest
from terrapod.services.workspace_autodiscovery_service import autodiscover_for_paths
from terrapod.storage import get_storage
from terrapod.storage.keys import config_version_key

logger = get_logger(__name__)

# Providers this module knows how to dispatch to. Keep in sync with
# `_parse_repo_url` below and with `_select_workspace_ids`'s Python-side
# filter — a new provider needs parser dispatch in both places.
_KNOWN_VCS_PROVIDERS = frozenset({"github", "gitlab"})


# --- Provider dispatch ---


def _parse_repo_url(conn: VCSConnection, repo_url: str) -> tuple[str, str] | None:
    """Parse a repo URL using the appropriate provider parser.

    Unknown providers are logged and return None — the github parser is
    permissive enough to tokenise a gitlab URL (and vice-versa), so an
    unknown provider must not silently fall through. Whoever adds a new
    provider needs to extend this dispatch (and ``_KNOWN_VCS_PROVIDERS``).
    """
    if conn.provider == "gitlab":
        return gitlab_service.parse_repo_url(repo_url)
    if conn.provider == "github":
        return github_service.parse_repo_url(repo_url)
    logger.warning(
        "Unknown VCS provider, cannot parse repo URL",
        provider=conn.provider,
        connection_id=str(conn.id),
    )
    return None


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


async def _list_repo_tree(conn: VCSConnection, owner: str, repo: str, ref: str) -> list[str] | None:
    """List every file path in the repo at `ref` via the appropriate provider.

    Used by the autodiscovery initial-scan path (#309). Returns None when
    the provider truncates / fails, signalling that the scan was
    incomplete; callers should NOT stamp `first_scan_at` in that case
    so the next poll cycle tries again.
    """
    if conn.provider == "gitlab":
        return await gitlab_service.list_repo_tree(conn, owner, repo, ref)
    return await github_service.list_repo_tree(conn, owner, repo, ref)


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


async def _stream_cv_upload_from_cache(
    cache_storage_key: str, workspace_id: uuid.UUID, cv_id: uuid.UUID
) -> None:
    """Materialise a cached VCS archive and stream-upload it to the workspace CV key.

    Uses temp files end-to-end — never holds the tarball in process memory.
    """
    storage = get_storage()
    cv_key = config_version_key(str(workspace_id), str(cv_id))

    async with materialize_archive(cache_storage_key) as path:
        from terrapod.services.vcs_archive_cache import _file_chunks

        await storage.put_stream(
            cv_key,
            _file_chunks(path),
            content_type="application/x-tar",
        )


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
    cache: VCSArchiveCache | None = None,
    fetch_paths: list[str] | None = None,
) -> Run | None:
    """Download archive (via cache + streaming), create ConfigurationVersion and Run.

    The `cache` argument coalesces concurrent downloads of the same (conn, sha)
    across workspace polls in the same cycle. Pass None when this path is
    invoked one-shot (e.g. UI-queued runs); a fresh cache instance is built
    internally so the streaming pipeline still applies.

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

    if cache is None:
        cache = VCSArchiveCache()

    try:
        cache_storage_key = await cache.get_or_fetch(conn, owner, repo, sha, paths=fetch_paths)
    except Exception as e:
        logger.error(
            "Failed to fetch repo archive into cache",
            workspace=ws.name,
            ref=sha[:8],
            error=str(e),
        )
        return None

    cv = await run_service.create_configuration_version(
        db,
        workspace_id=ws.id,
        source="vcs",
        auto_queue_runs=False,
        speculative=speculative,
    )
    await db.flush()

    try:
        await _stream_cv_upload_from_cache(cache_storage_key, ws.id, cv.id)
    except Exception as e:
        logger.error(
            "Failed to materialise cached archive into config version",
            workspace=ws.name,
            ref=sha[:8],
            cache_key=cache_storage_key,
            error=str(e),
        )
        return None

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
    cache: VCSArchiveCache | None = None,
    fetch_paths: list[str] | None = None,
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
        cache=cache,
        fetch_paths=fetch_paths,
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


async def _upsert_pr_session(
    db: AsyncSession,
    conn: VCSConnection,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> PRSession:
    """Find or create the PRSession row for this (connection, repo, PR).

    Updates `head_sha` if the PR has new commits. Idempotent — designed
    to be called every poll cycle that sees an open PR. Returns the
    persisted row (flushed but not committed; caller drives the commit).
    """
    existing = await db.execute(
        select(PRSession).where(
            PRSession.vcs_connection_id == conn.id,
            PRSession.repo == repo,
            PRSession.pr_number == pr_number,
        )
    )
    sess = existing.scalar_one_or_none()
    if sess is None:
        sess = PRSession(
            vcs_connection_id=conn.id,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            state="open",
        )
        db.add(sess)
        await db.flush()
        return sess
    if sess.head_sha != head_sha:
        sess.head_sha = head_sha
    if sess.state != "open":
        sess.state = "open"
    return sess


async def _poll_pr_comments(
    db: AsyncSession,
    conn: VCSConnection,
    repo: str,
) -> None:
    """Poll-fallback for `issue_comment` / `note` webhooks (#282).

    For each open PRSession on this (connection, repo), fetch comments
    since the last processed comment id and dispatch any `terrapod ...`
    commands via the scheduler. The scheduler's per-comment-id dedup
    key ensures webhook+poll racing doesn't double-dispatch.

    Required for deployments where the GitHub App doesn't have
    `issue_comment` webhook delivery (firewalled installs, local dev
    stacks, App permission upgrade not yet accepted) — webhooks
    accelerate, polling is the source of truth.
    """
    sessions = await db.execute(
        select(PRSession).where(
            PRSession.vcs_connection_id == conn.id,
            PRSession.repo == repo,
            PRSession.state == "open",
        )
    )
    open_sessions = list(sessions.scalars().all())
    if not open_sessions:
        return

    owner, repo_name = repo.split("/", 1)
    for sess in open_sessions:
        try:
            if conn.provider == "github":
                comments = await github_service.list_pr_comments_typed(
                    conn, owner, repo_name, sess.pr_number, since=None
                )
            elif conn.provider == "gitlab":
                comments = await gitlab_service.list_pr_comments_typed(
                    conn, owner, repo_name, sess.pr_number, since=None
                )
            else:
                continue
        except Exception as e:
            logger.warning(
                "comment poll: provider call failed",
                repo=repo,
                pr_number=sess.pr_number,
                error=str(e),
            )
            continue

        # Filter to comments newer than the last processed id (string
        # compare is fine — GitHub + GitLab comment ids are
        # monotonically increasing integers as strings).
        new_comments = [
            c
            for c in comments
            if sess.last_processed_comment_id is None
            or int(c.id) > int(sess.last_processed_comment_id)
        ]
        for c in new_comments:
            # Local import to avoid pulling the parser into the global
            # import graph for this single use.
            from terrapod.services.vcs_command_parser import is_command_comment

            if not is_command_comment(c.body):
                continue
            await enqueue_trigger(
                "vcs_comment_dispatch",
                {
                    "connection_id": str(conn.id),
                    "repo": repo,
                    "pr_number": sess.pr_number,
                    "comment_id": c.id,
                    "actor_login": c.author_login,
                    "actor_user_id": c.author_user_id,
                    "body": c.body,
                },
                dedup_key=f"vcs_cmd:{conn.id}:{repo}:{sess.pr_number}:{c.id}",
            )
            logger.info(
                "comment poll: dispatched terrapod command",
                repo=repo,
                pr_number=sess.pr_number,
                comment_id=c.id,
                author=c.author_login,
            )
        if new_comments:
            # Advance the cursor regardless of whether any comments
            # matched the parser — saves us re-scanning prose comments.
            sess.last_processed_comment_id = max((c.id for c in new_comments), key=lambda x: int(x))


async def _reconcile_closed_pr_sessions(
    db: AsyncSession,
    conn: VCSConnection,
    repo: str,
    open_pr_numbers: set[int],
) -> None:
    """Detect PRs that have been closed since the last poll and clean up.

    For each open PRSession on this (connection, repo) that's no longer
    in the VCS provider's open-PR list, cancel any active runs to release
    the workspace lock and mark the session as `closed`. This is the
    poll-cycle fallback for the `pull_request:closed` webhook (which
    phase 4 wires up). Hook-and-poll per #282: webhooks accelerate, polling
    is the source of truth.
    """
    sessions = await db.execute(
        select(PRSession).where(
            PRSession.vcs_connection_id == conn.id,
            PRSession.repo == repo,
            PRSession.state == "open",
        )
    )
    for sess in sessions.scalars().all():
        if sess.pr_number in open_pr_numbers:
            continue
        # PR no longer in the open list — cancel active runs, close session.
        active = await db.execute(
            select(Run).where(
                Run.vcs_pull_request_number == sess.pr_number,
                Run.status.notin_(run_service.TERMINAL_STATES),
            )
        )
        for run in active.scalars().all():
            try:
                await run_service.cancel_run(db, run, force=True)
                logger.info(
                    "Canceled run for closed PR",
                    run_id=str(run.id),
                    pr_number=sess.pr_number,
                    repo=repo,
                )
            except Exception as e:
                logger.warning(
                    "Failed to cancel run for closed PR",
                    run_id=str(run.id),
                    pr_number=sess.pr_number,
                    error=str(e),
                )
        sess.state = "closed"


async def _poll_workspace_prs(
    db: AsyncSession,
    ws: Workspace,
    conn: VCSConnection,
    owner: str,
    repo: str,
    branch: str,
    cache: VCSArchiveCache | None = None,
    fetch_paths: list[str] | None = None,
) -> None:
    """Check open PRs/MRs targeting the tracked branch for speculative plans."""
    prs = await _list_open_prs(conn, owner, repo, branch)

    # Hook-and-poll fallbacks (#282). Only run for apply-then-merge —
    # default-mode PR runs are plan-only and don't drive any of this.
    if ws.vcs_workflow == "apply_then_merge":
        open_pr_numbers = {pr.number for pr in prs}
        # PR-closed: cancel runs, release workspace locks.
        await _reconcile_closed_pr_sessions(db, conn, f"{owner}/{repo}", open_pr_numbers)
        # Comment polling: dispatch any new `terrapod ...` commands the
        # webhook either didn't deliver (no subscription, firewall) or
        # raced with this poll cycle (dedup key in dispatcher handles the race).
        await _poll_pr_comments(db, conn, f"{owner}/{repo}")

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
                await run_service.cancel_run(db, stale_run, force=True)
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

        # Branch on workspace mode (#282).
        # - merge_then_apply (default): PR run is speculative plan-only.
        # - apply_then_merge: PR run is a full plan-and-apply that saves
        #   the tfplan and sits in `planned` waiting on a user comment.
        #   The non-speculative run holds the workspace lock through
        #   `planned` (confirmed via Q8 in #282 — non-plan-only runs
        #   only release the lock at terminal/applied/cancelled).
        is_apply_then_merge = ws.vcs_workflow == "apply_then_merge"
        speculative = not is_apply_then_merge
        if is_apply_then_merge:
            message = f"Plan for PR #{pr.number}: {pr.title}"
        else:
            message = f"Speculative plan for PR #{pr.number}: {pr.title}"

        run = await _create_vcs_run(
            db,
            ws,
            conn,
            owner,
            repo,
            pr.head_sha,
            pr.head_ref,
            speculative=speculative,
            pr_number=pr.number,
            message=message,
            cache=cache,
            fetch_paths=fetch_paths,
        )

        if run:
            VCS_RUNS_CREATED.labels(provider=conn.provider, type="pr").inc()
            # For apply-then-merge, upsert the conversation-state row so
            # later phases (status comment, dispatcher) can hang state
            # off a stable PRSession id without re-querying the VCS.
            if is_apply_then_merge:
                sess = await _upsert_pr_session(db, conn, f"{owner}/{repo}", pr.number, pr.head_sha)
                # Fire the status-comment refresh asynchronously so the
                # poll cycle isn't blocked on the VCS API write.
                await enqueue_trigger(
                    "vcs_status_comment_update",
                    {"session_id": str(sess.id)},
                    dedup_key=f"vcs_status:{sess.id}",
                )
            await db.commit()
            logger.info(
                "Speculative run created for PR",
                workspace=ws.name,
                run_id=str(run.id),
                pr_number=pr.number,
                head_sha=pr.head_sha[:8],
            )


async def _poll_workspace(
    db: AsyncSession,
    ws: Workspace,
    cache: VCSArchiveCache | None = None,
    paths_unions: "PathsUnionMap | None" = None,
) -> None:
    """Poll a single workspace: check branch for pushes and PRs for speculative plans.

    `cache` coalesces concurrent (conn, sha, paths) fetches across workspaces
    in the same poll cycle. Pass None for one-shot calls; a fresh cache is
    built inside `_create_vcs_run` so streaming + per-call disk-temp
    behaviour still applies.

    `paths_unions` is the per-`(conn, owner, repo)` union map; we look up
    this workspace's group and pass the resolved path list to
    `_create_vcs_run` so the partial-clone fetch is narrowed.
    """
    if not ws.vcs_repo_url or not ws.vcs_connection_id:
        return

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if not conn or conn.status != "active":
        ws.vcs_last_error = "VCS connection is not active"
        ws.vcs_last_error_at = now_utc()
        logger.warning(
            "VCS connection not active",
            workspace=ws.name,
            connection_id=str(ws.vcs_connection_id),
        )
        return

    parsed = _parse_repo_url(conn, ws.vcs_repo_url)
    if not parsed:
        ws.vcs_last_error = f"Cannot parse VCS repo URL: {ws.vcs_repo_url}"
        ws.vcs_last_error_at = now_utc()
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
        ws.vcs_last_error_at = now_utc()
        return

    fetch_paths: list[str] | None = None
    if paths_unions is not None:
        fetch_paths = paths_unions.get((conn.id, owner, repo))

    try:
        # 1. Check tracked branch for new commits → real runs
        await _poll_workspace_branch(db, ws, conn, owner, repo, branch, cache, fetch_paths)

        # 2. Check open PRs/MRs targeting the tracked branch → speculative plans
        await _poll_workspace_prs(db, ws, conn, owner, repo, branch, cache, fetch_paths)

        # Success: update last-polled timestamp and clear any previous error
        ws.vcs_last_polled_at = now_utc()
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
        ws.vcs_last_error_at = now_utc()


# Bound the number of workspaces polled in parallel per cycle. Each workspace
# makes a handful of GitHub/GitLab API calls (branch SHA, list PRs, changed-files
# per PR); we cap concurrency so one cycle can't exhaust the provider's API
# rate limit or saturate the event loop with outstanding HTTP connections.
# Most deployments have <10 VCS workspaces per repo, so 10 covers the common
# case without bursting.
_MAX_PARALLEL_WORKSPACE_POLLS = 10


async def _poll_workspace_owned(
    ws_id: uuid.UUID,
    semaphore: asyncio.Semaphore,
    cache: VCSArchiveCache | None = None,
    paths_unions: "PathsUnionMap | None" = None,
) -> None:
    """Poll a single workspace in its own DB session, bounded by a semaphore.

    Each parallel poll needs its own session — AsyncSession is not safe to
    share across concurrent coroutines. Errors are caught and logged here
    so one workspace's failure doesn't sink the whole cycle.

    `cache` is shared across all workspaces in this cycle so concurrent polls
    of the same (conn, sha, paths) coalesce on a single fetch.

    `paths_unions` is the per-`(conn, owner, repo)` union of every
    workspace's `working_directory ∪ trigger_prefixes` for this cycle.
    Used to narrow the partial-clone fetch.
    """
    async with semaphore, get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        if ws is None:
            return
        try:
            await _poll_workspace(db, ws, cache, paths_unions)
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


async def _select_workspace_ids(
    db: AsyncSession,
    repo: str | None = None,
    provider: str | None = None,
) -> list[uuid.UUID]:
    """Fetch IDs of VCS-enabled workspaces, optionally filtered to one repo.

    The ``repo`` + ``provider`` filter is used by webhook-triggered
    immediate polls to avoid re-polling every workspace when only one
    repo on one provider had a push. ``repo`` is the ``owner/repo``
    form emitted by GitHub's ``repository.full_name`` (or GitLab's
    ``project.path_with_namespace``). ``provider`` narrows to
    ``"github"`` / ``"gitlab"`` — a GitHub webhook must not match a
    GitLab workspace whose URL happens to contain the same owner/repo
    slug (and vice-versa), because the two providers' parsers accept
    each other's URL shapes.

    We avoid fuzzy SQL matching on ``vcs_repo_url`` (which would risk
    wildcard-escape hazards and cross-org suffix collisions). Instead
    we load the VCS-enabled workspaces joined with their connection
    provider, parse each URL with its OWN provider's parser, and exact-
    match. Workspaces with VCS enabled typically number in the dozens
    to low hundreds — Python-side filtering is negligible and exact
    by construction.
    """
    stmt = (
        select(Workspace.id, Workspace.vcs_repo_url, VCSConnection.provider)
        .join(VCSConnection, Workspace.vcs_connection_id == VCSConnection.id)
        .where(
            Workspace.vcs_repo_url != "",
            Workspace.vcs_connection_id.isnot(None),
        )
    )
    if provider:
        stmt = stmt.where(VCSConnection.provider == provider)
    result = await db.execute(stmt)
    rows = list(result.all())
    if not repo:
        return [row[0] for row in rows]

    # Parse each workspace's URL with its OWN provider's parser. This is
    # the correctness guard: github's parser will happily tokenise a
    # gitlab.com URL as (owner, repo) — but we only want to consider
    # workspaces whose connection provider matches the webhook source.
    matched: list[uuid.UUID] = []
    target = repo  # already in "owner/repo" form
    for ws_id, url, ws_provider in rows:
        if ws_provider not in _KNOWN_VCS_PROVIDERS:
            # A provider column we don't know how to parse. Skip rather
            # than silently misparse with the github parser — whoever
            # adds a new provider needs to extend this dispatch.
            logger.warning(
                "Unknown VCS provider on workspace, skipping immediate-poll filter",
                workspace_id=str(ws_id),
                provider=ws_provider,
            )
            continue
        parse = (
            gitlab_service.parse_repo_url
            if ws_provider == "gitlab"
            else github_service.parse_repo_url
        )
        parsed = parse(url)
        if parsed is None:
            continue
        owner, repo_name = parsed
        if f"{owner}/{repo_name}" == target:
            matched.append(ws_id)
    return matched


PathsUnionMap = dict[tuple[uuid.UUID, str, str], list[str]]


async def _compute_paths_unions(db: AsyncSession, workspace_ids: list[uuid.UUID]) -> PathsUnionMap:
    """Compute the union of working_directory + trigger_prefixes per
    (connection_id, owner, repo) across the cycle's workspaces.

    Returned map is keyed by (connection_id, owner, repo); the value is
    the sorted, deduplicated, prefix-collapsed path list. An empty list
    means "fetch the whole repo" (some workspace under that key has no
    narrowing configured).

    This drives the partial-clone fetch in `git_fetch.py`. Path narrowing
    only applies when EVERY workspace under the same (conn, owner, repo)
    has a non-empty `working_directory ∪ trigger_prefixes`. If any one
    workspace wants the whole repo, we fetch the whole repo for that
    cache entry — otherwise that workspace's plan/apply would miss
    files outside its declared paths.
    """
    if not workspace_ids:
        return {}
    stmt = (
        select(
            Workspace.id,
            Workspace.vcs_connection_id,
            Workspace.vcs_repo_url,
            Workspace.working_directory,
            Workspace.trigger_prefixes,
            VCSConnection.provider,
        )
        .join(VCSConnection, Workspace.vcs_connection_id == VCSConnection.id)
        .where(Workspace.id.in_(workspace_ids))
    )
    rows = (await db.execute(stmt)).all()

    # Per-key accumulator. None as the value means "whole repo wanted by
    # at least one workspace under this key" — we collapse to [] at the end.
    accum: dict[tuple[uuid.UUID, str, str], set[str] | None] = {}
    for _ws_id, conn_id, repo_url, wd, tp, provider in rows:
        if conn_id is None or not repo_url:
            continue
        if provider not in _KNOWN_VCS_PROVIDERS:
            continue
        parse = (
            gitlab_service.parse_repo_url if provider == "gitlab" else github_service.parse_repo_url
        )
        parsed = parse(repo_url)
        if parsed is None:
            continue
        owner, repo_name = parsed
        key = (conn_id, owner, repo_name)

        ws_paths: list[str] = []
        if wd:
            ws_paths.append(wd.strip("/ "))
        if tp:
            ws_paths.extend(p.strip("/ ") for p in tp if p)
        ws_paths = [p for p in ws_paths if p]

        if not ws_paths:
            # Workspace wants the whole repo → poison the union for this key.
            # Trade-off: any other workspace under the same (conn, owner, repo)
            # could in principle have narrowed independently, but they'd then
            # need their own cache entry — losing the cross-workspace fetch
            # coalescing this map exists to provide. Correctness wins: we
            # fetch the union (= whole repo) once and serve all of them.
            accum[key] = None
            continue
        existing = accum.get(key, set())
        if existing is None:
            continue  # already poisoned
        existing.update(ws_paths)
        accum[key] = existing

    out: PathsUnionMap = {}
    for key, val in accum.items():
        if val is None:
            out[key] = []  # whole-repo sentinel
        else:
            # Drop entries that are strict prefixes of others (the shorter
            # subsumes). git_fetch.normalize_paths does the same; we duplicate
            # here so the map values are already canonical for logging.
            sorted_paths = sorted(val)
            collapsed: list[str] = []
            for p in sorted_paths:
                if any(p != prev and p.startswith(prev + "/") for prev in collapsed):
                    continue
                collapsed.append(p)
            out[key] = collapsed
    return out


async def _poll_autodiscovery_for_connection(
    db: AsyncSession,
    conn: VCSConnection,
    rules: list[AutodiscoveryRule],
) -> int:
    """Scan open PRs for one connection's rules and auto-create workspaces.

    Returns the number of workspaces created. Per-connection scoping
    means a single bad connection (auth issue, GitHub rate limit) does
    not block other connections in the same cycle.
    """
    # Group rules by (repo_url, branch) so we don't re-fetch the same
    # PR list once per rule.
    by_repo_branch: dict[tuple[str, str], list[AutodiscoveryRule]] = {}
    for rule in rules:
        by_repo_branch.setdefault((rule.repo_url, rule.branch or ""), []).append(rule)

    created_count = 0
    for (repo_url, branch), group in by_repo_branch.items():
        owner_repo = _parse_repo_url(conn, repo_url)
        if owner_repo is None:
            logger.warning(
                "Autodiscovery: cannot parse repo URL",
                connection_id=str(conn.id),
                repo_url=repo_url,
            )
            continue
        owner, repo = owner_repo

        # Resolve the default branch if the rule didn't pin one.
        target_branch = branch
        if not target_branch:
            try:
                target_branch = await _get_default_branch(conn, owner, repo) or "main"
            except Exception:
                logger.warning(
                    "Autodiscovery: failed to resolve default branch — skipping",
                    connection_id=str(conn.id),
                    repo_url=repo_url,
                    exc_info=True,
                )
                continue

        # Pull open PRs and the default-branch tip — both are sources of
        # discoverable changed files.
        try:
            prs = await _list_open_prs(conn, owner, repo, target_branch)
        except Exception:
            logger.warning(
                "Autodiscovery: failed to list PRs — skipping",
                connection_id=str(conn.id),
                repo_url=repo_url,
                branch=target_branch,
                exc_info=True,
            )
            continue

        for pr in prs:
            try:
                changed = await _get_changed_files(conn, owner, repo, target_branch, pr.head_sha)
            except Exception:
                logger.warning(
                    "Autodiscovery: failed to get changed files for PR — skipping",
                    connection_id=str(conn.id),
                    repo_url=repo_url,
                    pr_number=pr.number,
                    exc_info=True,
                )
                continue
            new_workspaces = await autodiscover_for_paths(db, group, changed)
            created_count += len(new_workspaces)

        # Initial-scan path (#309): rules with `first_scan_at IS NULL`
        # have never been backfilled, so this poll cycle does a one-time
        # full-tree walk of the target branch and feeds every file path
        # to the matcher. After a successful walk, stamp `first_scan_at`
        # so we don't repeat the walk every cycle.
        #
        # Failures (`None` return) leave `first_scan_at` untouched so
        # the next cycle retries. The change-driven walk above keeps
        # working in the meantime — initial-scan failure doesn't block
        # ongoing autodiscovery, it just delays the backfill.
        unscanned = [r for r in group if r.first_scan_at is None]
        if unscanned:
            try:
                all_files = await _list_repo_tree(conn, owner, repo, target_branch)
            except Exception:
                logger.warning(
                    "Autodiscovery: initial-scan tree fetch failed — will retry next cycle",
                    connection_id=str(conn.id),
                    repo_url=repo_url,
                    ref=target_branch,
                    exc_info=True,
                )
                all_files = None
            if all_files is not None:
                new_workspaces = await autodiscover_for_paths(db, unscanned, all_files)
                created_count += len(new_workspaces)
                scanned_at = now_utc()
                for rule in unscanned:
                    rule.first_scan_at = scanned_at
                await db.commit()
                logger.info(
                    "Autodiscovery: initial scan complete",
                    connection_id=str(conn.id),
                    repo_url=repo_url,
                    ref=target_branch,
                    rules_scanned=len(unscanned),
                    workspaces_created=len(new_workspaces),
                    files_walked=len(all_files),
                )

    return created_count


async def _poll_autodiscovery(
    *, owner_repo: tuple[str, str] | None = None, provider: str | None = None
) -> int:
    """Run autodiscovery across every VCS connection that has at least
    one enabled rule.

    Auto-created workspaces are picked up by the *next* poll cycle's
    workspace scan, which queues their first speculative run via the
    existing PR/branch logic. We don't queue runs from here directly —
    keeps the autodiscovery pass narrow (just creates workspaces) and
    leaves run-creation to the existing well-tested code path.

    `owner_repo` + `provider` filter rules to a single repo — used by
    the webhook-triggered immediate poll so we don't fan out to every
    connection on every webhook.
    """
    async with get_db_session() as db:
        # Eager-load the connection so the per-rule code path doesn't
        # need a second round-trip per connection.
        result = await db.execute(
            select(AutodiscoveryRule)
            .where(AutodiscoveryRule.enabled.is_(True))
            .order_by(AutodiscoveryRule.vcs_connection_id, AutodiscoveryRule.id)
        )
        rules = list(result.scalars().all())

        # Webhook fast-path: keep only rules whose repo matches the
        # webhook source. We do the filter Python-side because the
        # repo URL form (https vs git@) and trailing-slash semantics
        # differ across providers.
        if owner_repo is not None:
            owner, repo = owner_repo
            match_pairs: set[tuple[str, str]] = {(owner, repo), (owner.lower(), repo.lower())}
            filtered: list[AutodiscoveryRule] = []
            for rule in rules:
                conn = rule.vcs_connection
                if provider is not None and conn is not None and conn.provider != provider:
                    continue
                if conn is None:
                    continue
                parsed = _parse_repo_url(conn, rule.repo_url)
                if parsed is None:
                    continue
                if parsed in match_pairs or (parsed[0].lower(), parsed[1].lower()) in match_pairs:
                    filtered.append(rule)
            rules = filtered
        if not rules:
            return 0

        # Group rules by connection so we open one VCSConnection load per
        # connection rather than per rule.
        by_conn: dict[uuid.UUID, list[AutodiscoveryRule]] = {}
        for rule in rules:
            by_conn.setdefault(rule.vcs_connection_id, []).append(rule)

        total_created = 0
        for conn_id, conn_rules in by_conn.items():
            conn = await db.get(VCSConnection, conn_id)
            if conn is None or conn.status != "active":
                continue
            try:
                created = await _poll_autodiscovery_for_connection(db, conn, conn_rules)
                total_created += created
            except Exception:
                # Per-connection isolation: log and continue.
                logger.warning(
                    "Autodiscovery: connection scan failed",
                    connection_id=str(conn_id),
                    exc_info=True,
                )

        if total_created:
            logger.info(
                "Autodiscovery created workspaces this cycle",
                count=total_created,
            )
        return total_created


async def poll_cycle() -> None:
    """Execute one poll cycle: check all VCS-enabled workspaces in parallel.

    Called by the distributed scheduler as a periodic task. Only one
    replica runs this per interval across the entire deployment.
    """
    start = time_mod.monotonic()

    # Autodiscovery first — any newly-created workspaces will be
    # picked up by the workspace scan below (or, if their query
    # snapshot already ran, by the next cycle).
    try:
        await _poll_autodiscovery()
    except Exception:
        logger.warning("Autodiscovery pass failed", exc_info=True)

    async with get_db_session() as db:
        workspace_ids = await _select_workspace_ids(db)
        paths_unions = await _compute_paths_unions(db, workspace_ids)

    if not workspace_ids:
        VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)
        return

    logger.debug(
        "VCS poll cycle",
        workspace_count=len(workspace_ids),
        union_groups=len(paths_unions),
    )

    # One cache instance per cycle — coalesces concurrent (conn, sha, paths)
    # fetches across all workspace polls in this cycle.
    cache = VCSArchiveCache()
    semaphore = asyncio.Semaphore(_MAX_PARALLEL_WORKSPACE_POLLS)
    await asyncio.gather(
        *[_poll_workspace_owned(wid, semaphore, cache, paths_unions) for wid in workspace_ids],
        return_exceptions=True,
    )

    VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)


async def handle_immediate_poll(payload: dict) -> None:
    """Handle a webhook-triggered immediate poll for a specific repo.

    Called by the distributed scheduler's trigger consumer. The payload
    contains ``{"repo": "owner/repo", "provider": "github"}`` from the
    webhook handler (older payloads without ``provider`` are treated as
    GitHub for back-compat, since that's the only webhook shipped before
    this field was added). We poll only the workspaces whose connection
    matches the webhook source.
    """
    start = time_mod.monotonic()
    repo = payload.get("repo", "")
    # Back-compat: pre-PR enqueues didn't carry "provider". At the time,
    # github was the only webhook receiver, so defaulting there matches
    # historical behaviour for any in-flight triggers.
    provider = payload.get("provider", "github")
    logger.info("Immediate poll triggered by webhook", repo=repo, provider=provider)

    # Run autodiscovery scoped to this repo first so any newly-created
    # workspaces get picked up by the workspace scan that follows. The
    # repo string in the webhook payload is "owner/repo" — split for
    # the autodiscovery filter.
    if repo and "/" in repo:
        owner, repo_name = repo.split("/", 1)
        try:
            await _poll_autodiscovery(owner_repo=(owner, repo_name), provider=provider)
        except Exception:
            logger.warning(
                "Autodiscovery: webhook-triggered scan failed",
                repo=repo,
                exc_info=True,
            )

    async with get_db_session() as db:
        workspace_ids = await _select_workspace_ids(
            db,
            repo=repo or None,
            provider=provider,
        )
        paths_unions = await _compute_paths_unions(db, workspace_ids)

    if not workspace_ids:
        logger.info("Immediate poll: no workspaces match repo", repo=repo)
        VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)
        return

    logger.info(
        "Immediate poll: polling matching workspaces",
        repo=repo,
        workspace_count=len(workspace_ids),
    )

    # Webhook-triggered polls also share one cache instance — same coalescing
    # benefit when multiple workspaces map to the same repo.
    cache = VCSArchiveCache()
    semaphore = asyncio.Semaphore(_MAX_PARALLEL_WORKSPACE_POLLS)
    await asyncio.gather(
        *[_poll_workspace_owned(wid, semaphore, cache, paths_unions) for wid in workspace_ids],
        return_exceptions=True,
    )

    VCS_POLL_DURATION.labels(provider="all").observe(time_mod.monotonic() - start)
