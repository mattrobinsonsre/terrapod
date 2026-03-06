"""Triggered task handler for VCS commit status and PR comment posting.

Registered with the distributed scheduler as a trigger handler.
Receives {run_id, workspace_id, target_status} payloads and posts
commit statuses and PR/MR comments back to the VCS provider.
"""

import uuid
from datetime import UTC, datetime

from terrapod.config import settings
from terrapod.db.models import Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service

logger = get_logger(__name__)

# Run status → (github_state, gitlab_state, description)
_STATUS_MAP: dict[str, tuple[str, str, str]] = {
    "pending": ("pending", "pending", "Run queued"),
    "queued": ("pending", "pending", "Waiting for runner"),
    "planning": ("pending", "running", "Plan in progress"),
    "applying": ("pending", "running", "Apply in progress"),
    "applied": ("success", "success", "Apply complete"),
    "errored": ("failure", "failed", "Run failed"),
    "discarded": ("failure", "failed", "Plan discarded"),
    "canceled": ("error", "canceled", "Run canceled"),
}

# Status → emoji for PR comments
_STATUS_EMOJI: dict[str, str] = {
    "pending": ":hourglass:",
    "queued": ":hourglass:",
    "planning": ":gear:",
    "planned": ":white_check_mark:",
    "applying": ":rocket:",
    "applied": ":white_check_mark:",
    "errored": ":x:",
    "discarded": ":no_entry_sign:",
    "canceled": ":stop_sign:",
}

# Redis key prefix for caching PR comment IDs
_COMMENT_CACHE_PREFIX = "tp:vcs_comment:"
_COMMENT_CACHE_TTL = 7 * 24 * 3600  # 7 days


def _resolve_status(run_status: str, plan_only: bool) -> tuple[str, str, str]:
    """Map run status to (github_state, gitlab_state, description).

    Special case: 'planned' status depends on whether the run is plan-only.
    """
    if run_status == "planned":
        if plan_only:
            return ("success", "success", "Plan finished")
        return ("pending", "running", "Plan complete, awaiting confirmation")
    return _STATUS_MAP.get(run_status, ("pending", "pending", run_status))


def _build_comment_body(
    workspace_name: str,
    workspace_id: str,
    run_id: str,
    run_status: str,
    plan_only: bool,
    has_changes: bool | None,
    run_url: str,
) -> str:
    """Build the markdown body for a PR/MR comment."""
    github_state, _, description = _resolve_status(run_status, plan_only)
    emoji = _STATUS_EMOJI.get(run_status, ":grey_question:")

    has_changes_line = ""
    if run_status == "planned" and has_changes is not None:
        if has_changes:
            has_changes_line = "Plan has changes — review in Terrapod."
        else:
            has_changes_line = "No changes detected."

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        f"<!-- terrapod:ws:{workspace_id} -->",
        f"### Terrapod — {workspace_name}",
        "",
        f"**Status:** {emoji} {description}",
        f"**Run:** [{run_id}]({run_url})",
    ]
    if has_changes_line:
        lines.append(has_changes_line)
    lines += ["", f"*Updated {now}*"]

    return "\n".join(lines)


def _comment_marker(workspace_id: str) -> str:
    return f"<!-- terrapod:ws:{workspace_id} -->"


async def _find_or_create_comment(
    conn: VCSConnection,
    owner: str,
    repo: str,
    pr_number: int,
    workspace_id: str,
    body: str,
) -> None:
    """Find an existing comment by marker, update it, or create a new one.

    Uses Redis cache for comment ID, falls back to listing comments.
    """
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    cache_key = f"{_COMMENT_CACHE_PREFIX}{workspace_id}:{pr_number}"
    marker = _comment_marker(workspace_id)

    # Try cached comment ID
    cached_id = await redis.get(cache_key)
    if cached_id:
        comment_id = int(cached_id)
        try:
            if conn.provider == "gitlab":
                await gitlab_service.update_mr_comment(
                    conn, owner, repo, pr_number, comment_id, body
                )
            else:
                await github_service.update_pr_comment(conn, owner, repo, comment_id, body)
            await redis.set(cache_key, str(comment_id), ex=_COMMENT_CACHE_TTL)
            return
        except Exception:
            # Cache stale — fall through to search
            logger.debug("Cached comment ID stale, searching", comment_id=comment_id)

    # Search for existing comment with marker
    comment_id = None
    try:
        if conn.provider == "gitlab":
            comments = await gitlab_service.list_mr_comments(conn, owner, repo, pr_number)
        else:
            comments = await github_service.list_pr_comments(conn, owner, repo, pr_number)

        for c in comments:
            c_body = c.get("body", "")
            if marker in c_body:
                comment_id = c["id"]
                break
    except Exception as e:
        logger.warning("Failed to list PR comments for marker search", error=str(e))

    if comment_id:
        # Update existing
        try:
            if conn.provider == "gitlab":
                await gitlab_service.update_mr_comment(
                    conn, owner, repo, pr_number, comment_id, body
                )
            else:
                await github_service.update_pr_comment(conn, owner, repo, comment_id, body)
            await redis.set(cache_key, str(comment_id), ex=_COMMENT_CACHE_TTL)
            return
        except Exception as e:
            logger.warning("Failed to update PR comment", error=str(e))

    # Create new
    try:
        if conn.provider == "gitlab":
            new_id = await gitlab_service.create_mr_comment(conn, owner, repo, pr_number, body)
        else:
            new_id = await github_service.create_pr_comment(conn, owner, repo, pr_number, body)
        await redis.set(cache_key, str(new_id), ex=_COMMENT_CACHE_TTL)
    except Exception as e:
        logger.warning("Failed to create PR comment", error=str(e))


async def handle_vcs_commit_status(payload: dict) -> None:
    """Handle a VCS commit status trigger.

    Posts commit status and optionally a PR/MR comment.
    """
    run_id_str = payload.get("run_id", "")
    workspace_id_str = payload.get("workspace_id", "")
    target_status = payload.get("target_status", "")

    if not run_id_str or not workspace_id_str or not target_status:
        logger.warning("Incomplete VCS status payload", payload=payload)
        return

    async with get_db_session() as db:
        run = await db.get(Run, uuid.UUID(run_id_str))
        ws = await db.get(Workspace, uuid.UUID(workspace_id_str))

        if run is None or ws is None:
            logger.warning(
                "Run or workspace not found for VCS status",
                run_id=run_id_str,
                workspace_id=workspace_id_str,
            )
            return

        if not run.vcs_commit_sha:
            return

        if not ws.vcs_connection_id or not ws.vcs_repo_url:
            return

        conn = await db.get(VCSConnection, ws.vcs_connection_id)
        if not conn or conn.status != "active":
            logger.warning(
                "VCS connection not active for status posting",
                connection_id=str(ws.vcs_connection_id),
            )
            return

        # Parse repo URL
        if conn.provider == "gitlab":
            parsed = gitlab_service.parse_repo_url(ws.vcs_repo_url)
        else:
            parsed = github_service.parse_repo_url(ws.vcs_repo_url)

        if not parsed:
            logger.warning("Cannot parse VCS repo URL", url=ws.vcs_repo_url)
            return

        owner, repo = parsed

        # Resolve status
        github_state, gitlab_state, description = _resolve_status(target_status, run.plan_only)

        # Build target URL
        target_url = ""
        if settings.external_url:
            target_url = f"{settings.external_url.rstrip('/')}/workspaces/{ws.id}/runs/{run.id}"

        # Post commit status
        try:
            if conn.provider == "gitlab":
                await gitlab_service.create_commit_status(
                    conn,
                    owner,
                    repo,
                    run.vcs_commit_sha,
                    state=gitlab_state,
                    description=description,
                    target_url=target_url,
                )
            else:
                await github_service.create_commit_status(
                    conn,
                    owner,
                    repo,
                    run.vcs_commit_sha,
                    state=github_state,
                    description=description,
                    target_url=target_url,
                )
        except Exception as e:
            logger.warning(
                "Failed to post VCS commit status",
                run_id=run_id_str,
                error=str(e),
            )

        # Post/update PR comment (only for PR runs)
        if run.vcs_pull_request_number:
            run_url = target_url or f"run-{run.id}"
            body = _build_comment_body(
                workspace_name=ws.name,
                workspace_id=str(ws.id),
                run_id=f"run-{run.id}",
                run_status=target_status,
                plan_only=run.plan_only,
                has_changes=run.has_changes,
                run_url=run_url,
            )
            await _find_or_create_comment(
                conn, owner, repo, run.vcs_pull_request_number, str(ws.id), body
            )

    logger.info(
        "VCS commit status posted",
        run_id=run_id_str,
        status=target_status,
        workspace=ws.name if ws else workspace_id_str,
    )
