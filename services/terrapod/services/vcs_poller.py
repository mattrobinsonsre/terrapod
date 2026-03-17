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

import io
import tarfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, run_service
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


def _changes_affect_directory(changed_files: list[str], working_directory: str) -> bool:
    """Check if any changed files fall within the workspace's working directory.

    Uses strict prefix matching: only files starting with "{working_directory}/"
    are considered relevant. Root-level files don't trigger subdirectory workspaces.
    """
    prefix = working_directory.rstrip("/") + "/"
    return any(f.startswith(prefix) for f in changed_files)


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


def _strip_top_level_dir(archive: bytes) -> bytes:
    """Repack a tarball, stripping the single top-level directory.

    GitHub/GitLab tarballs wrap content in a directory like
    ``owner-repo-sha/``.  The runner entrypoint extracts to /workspace
    and expects .tf files at the root, so we strip the wrapper.
    """
    in_buf = io.BytesIO(archive)
    out_buf = io.BytesIO()

    with (
        tarfile.open(fileobj=in_buf, mode="r:gz") as src,
        tarfile.open(fileobj=out_buf, mode="w:gz") as dst,
    ):
        for member in src.getmembers():
            # Strip first path component: "owner-repo-sha/file" → "file"
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue  # skip the top-level directory entry itself
            member.name = parts[1]
            if member.isfile():
                f = src.extractfile(member)
                if f:
                    dst.addfile(member, f)
            else:
                dst.addfile(member)

    return out_buf.getvalue()


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
    """Download archive, create ConfigurationVersion and Run."""
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
    archive = _strip_top_level_dir(archive)

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
    try:
        sha = await _get_branch_sha(conn, owner, repo, branch)
    except Exception as e:
        logger.error(
            "Failed to get branch SHA",
            workspace=ws.name,
            repo=f"{owner}/{repo}",
            branch=branch,
            error=str(e),
        )
        return

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

    logger.info(
        "New commit detected",
        workspace=ws.name,
        repo=f"{owner}/{repo}",
        branch=branch,
        old_sha=ws.vcs_last_commit_sha[:8] if ws.vcs_last_commit_sha else "(none)",
        new_sha=sha[:8],
    )

    # VCS subdirectory filtering: skip runs when changes don't affect the workspace
    if ws.vcs_working_directory and ws.vcs_last_commit_sha:
        try:
            changed = await _get_changed_files(conn, owner, repo, ws.vcs_last_commit_sha, sha)
            # None means truncated — create run unconditionally
            if changed is not None and not _changes_affect_directory(
                changed, ws.vcs_working_directory
            ):
                logger.info(
                    "Skipping run — no changes in working directory",
                    workspace=ws.name,
                    working_directory=ws.vcs_working_directory,
                    changed_files_count=len(changed),
                )
                ws.vcs_last_commit_sha = sha
                await db.commit()
                return
        except Exception as e:
            logger.warning(
                "Failed to get changed files, creating run anyway",
                workspace=ws.name,
                error=str(e),
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
        ws.vcs_last_commit_sha = sha
        await db.commit()

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
    try:
        prs = await _list_open_prs(conn, owner, repo, branch)
    except Exception as e:
        logger.error(
            "Failed to list PRs",
            workspace=ws.name,
            repo=f"{owner}/{repo}",
            error=str(e),
        )
        return

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

        logger.info(
            "New PR commit detected",
            workspace=ws.name,
            pr_number=pr.number,
            head_ref=pr.head_ref,
            head_sha=pr.head_sha[:8],
            title=pr.title,
        )

        # VCS subdirectory filtering for PRs: compare PR head against tracked branch
        if ws.vcs_working_directory and ws.vcs_last_commit_sha:
            try:
                changed = await _get_changed_files(
                    conn, owner, repo, ws.vcs_last_commit_sha, pr.head_sha
                )
                # None means truncated — create run unconditionally
                if changed is not None and not _changes_affect_directory(
                    changed, ws.vcs_working_directory
                ):
                    logger.info(
                        "Skipping PR run — no changes in working directory",
                        workspace=ws.name,
                        pr_number=pr.number,
                        working_directory=ws.vcs_working_directory,
                    )
                    continue
            except Exception as e:
                logger.warning(
                    "Failed to get changed files for PR, creating run anyway",
                    workspace=ws.name,
                    pr_number=pr.number,
                    error=str(e),
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
        logger.warning(
            "VCS connection not active",
            workspace=ws.name,
            connection_id=str(ws.vcs_connection_id),
        )
        return

    parsed = _parse_repo_url(conn, ws.vcs_repo_url)
    if not parsed:
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
        return

    # 1. Check tracked branch for new commits → real runs
    await _poll_workspace_branch(db, ws, conn, owner, repo, branch)

    # 2. Check open PRs/MRs targeting the tracked branch → speculative plans
    await _poll_workspace_prs(db, ws, conn, owner, repo, branch)


async def poll_cycle() -> None:
    """Execute one poll cycle: check all VCS-enabled workspaces.

    Called by the distributed scheduler as a periodic task. Only one
    replica runs this per interval across the entire deployment.
    """
    async with get_db_session() as db:
        result = await db.execute(
            select(Workspace).where(
                Workspace.vcs_repo_url != "",
                Workspace.vcs_connection_id.isnot(None),
            )
        )
        workspaces = result.scalars().all()

        if not workspaces:
            return

        logger.debug("VCS poll cycle", workspace_count=len(workspaces))

        for ws in workspaces:
            try:
                await _poll_workspace(db, ws)
            except Exception as e:
                logger.error(
                    "Error polling workspace",
                    workspace=ws.name,
                    error=str(e),
                    exc_info=e,
                )


async def handle_immediate_poll(payload: dict) -> None:
    """Handle a webhook-triggered immediate poll for a specific repo.

    Called by the distributed scheduler's trigger consumer. The payload
    contains {"repo": "owner/repo"} from the webhook handler.
    Runs a full poll cycle (the repo filter is informational — we poll
    all workspaces since multiple workspaces may track the same repo).
    """
    repo = payload.get("repo", "unknown")
    logger.info("Immediate poll triggered by webhook", repo=repo)
    await poll_cycle()
