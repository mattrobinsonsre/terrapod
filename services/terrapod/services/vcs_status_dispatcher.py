"""Triggered task handler for VCS commit status and PR comment posting.

Registered with the distributed scheduler as a trigger handler.
Receives {run_id, workspace_id, target_status} payloads and posts
commit statuses and PR/MR comments back to the VCS provider.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from terrapod.config import settings
from terrapod.db.models import PlanSummary, Run, VCSConnection, Workspace
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


def _resolve_status(
    run_status: str, plan_only: bool, has_changes: bool | None = None
) -> tuple[str, str, str]:
    """Map run status to (github_state, gitlab_state, description).

    Plan-only 'planned' runs use the plan's has_changes flag to produce a
    descriptive message ("Has changes" / "No changes") instead of the generic
    "Plan finished". The check still reports success in both cases — only the
    text differs. Non-plan-only 'planned' runs keep the awaiting-confirmation
    pending state, annotated with has-changes when known. When has_changes
    is None (older runs, pre-plan statuses) the description falls back to
    the bare form.
    """
    if run_status == "planned":
        # A no-op plan is effectively done — nothing to apply, nothing to
        # confirm. Report success regardless of plan_only.
        if has_changes is False:
            return ("success", "success", "No changes")
        if plan_only:
            if has_changes is True:
                return ("success", "success", "Has changes")
            return ("success", "success", "Plan finished")
        if has_changes is True:
            return ("pending", "running", "Has changes, awaiting confirmation")
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
    ai_summary: "PlanSummary | None" = None,
) -> str:
    """Build the markdown body for a PR/MR comment.

    `has_changes` is consumed by ``_resolve_status`` to form the status
    description ("Has changes" / "No changes"); we don't append a second
    has-changes line, which used to be redundant with the description.

    When ``ai_summary`` is provided and its status is "ready", a
    collapsible ``<details>`` block is appended with the LLM-generated
    description + risk pill + risk-factor list (#401). Other statuses
    (pending / skipped / errored) are skipped — the comment shouldn't
    advertise empty or error rows to PR readers.
    """
    github_state, _, description = _resolve_status(run_status, plan_only, has_changes)
    emoji = _STATUS_EMOJI.get(run_status, ":grey_question:")

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    parts = [
        f"<!-- terrapod:ws:{workspace_id} -->",
        f"### Terrapod — {workspace_name}",
        "",
        f"**Status:** {emoji} {description}",
        f"**Run:** [{run_id}]({run_url})",
    ]
    if ai_summary is not None and ai_summary.status == "ready":
        parts.append("")
        parts.append(_render_ai_summary_section(ai_summary))
    parts.append("")
    parts.append(f"*Updated {now}*")
    return "\n".join(parts)


def _render_ai_summary_section(s: "PlanSummary") -> str:
    """Render one PlanSummary as a collapsible PR-comment section (#401).

    Both GitHub and GitLab support raw HTML inside Markdown comments;
    `<details>` keeps verbose summaries out of the default view so PRs
    spanning many workspaces stay scannable. The summary header carries
    the risk pill so a reviewer can triage without expanding.
    """
    kind_label = "Failure analysis" if s.kind == "failure_analysis" else "AI summary"
    risk_emoji = {
        "low": "🟢",
        "medium": "🟡",
        "high": "🟠",
        "critical": "🔴",
    }.get(s.risk_level, "⚪")
    lines = [
        f"<details><summary>🤖 {kind_label} &mdash; "
        f"{risk_emoji} risk: <strong>{s.risk_level or 'unknown'}</strong></summary>",
        "",
        s.description.strip(),
    ]
    if s.risk_factors:
        rf_heading = "**Suggested fixes:**" if s.kind == "failure_analysis" else "**Risk factors:**"
        lines.extend(["", rf_heading, ""])
        for rf in s.risk_factors:
            sev = (rf.get("severity") or "").lower() if isinstance(rf, dict) else ""
            sev_emoji = {
                "low": "🟢",
                "medium": "🟡",
                "high": "🟠",
                "critical": "🔴",
            }.get(sev, "⚪")
            title = rf.get("title", "") if isinstance(rf, dict) else ""
            detail = (rf.get("detail", "") or "").strip() if isinstance(rf, dict) else ""
            addr = rf.get("resource_address", "") if isinstance(rf, dict) else ""
            head = f"- {sev_emoji} **{title}**"
            if addr:
                head += f" — `{addr}`"
            lines.append(head)
            if detail:
                lines.append(f"  {detail}")
    lines.extend(["", "</details>"])
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

    ``has_changes`` is expected to be present in the payload — the enqueuer
    snapshots it at the moment of status transition so we don't depend on
    when the run row's ``has_changes`` column lands in the DB (a trigger
    consumed by another replica can otherwise outrun the commit).
    """
    run_id_str = payload.get("run_id", "")
    workspace_id_str = payload.get("workspace_id", "")
    target_status = payload.get("target_status", "")
    # has_changes is tri-state (True / False / None). We need to tell
    # "payload didn't carry the key" (fall back to DB) apart from
    # "payload carried None" (truly unknown — skip the fallback). A
    # sentinel captures this without leaking `object` into the type.
    _UNSET: object = object()
    payload_has_changes: bool | None | object = payload.get("has_changes", _UNSET)

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

        # Prefer the payload-carried has_changes — it was snapshotted at
        # the moment of the status transition, before the trigger was
        # enqueued, so it's never stale. Only fall back to the DB value
        # when the payload omits the key (older enqueuers, or a replay
        # from a queue entry written by pre-fix code).
        has_changes: bool | None = (
            payload_has_changes  # type: ignore[assignment]
            if payload_has_changes is not _UNSET
            else run.has_changes
        )
        github_state, gitlab_state, description = _resolve_status(
            target_status, run.plan_only, has_changes
        )

        # Build target URL
        target_url = ""
        if settings.external_url:
            target_url = f"{settings.external_url.rstrip('/')}/workspaces/{ws.id}/runs/{run.id}"

        # Scope context to the workspace so multiple workspaces linked to the
        # same PR (e.g. module-impact fan-out) each get a distinct check,
        # rather than clobbering each other.
        context = f"terrapod/{ws.name}"

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
                    context=context,
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
                    context=context,
                )
        except Exception as e:
            # ERROR (not warning) because by the time we reach this except,
            # the underlying transport-level retry has already exhausted
            # (3 retries for both GitHub `_github_request` and GitLab
            # `_gitlab_request`). The handler has nothing left to fall
            # back to — the commit status / PR check stays in its previous
            # state, which is the user-visible symptom from #360. Bumping
            # to error makes it visible in monitoring; alerting on this
            # gives operators a chance to chase intermittent VCS-API
            # incidents before users notice.
            logger.error(
                "Failed to post VCS commit status (transport retries exhausted)",
                run_id=run_id_str,
                workspace_id=workspace_id_str,
                provider=conn.provider,
                target_status=target_status,
                sha=run.vcs_commit_sha[:12] if run.vcs_commit_sha else "",
                error=str(e),
            )

        # Post/update PR comment (only for PR runs).
        #
        # The comment is shared across every run for this (workspace, PR)
        # tuple — keyed by `<!-- terrapod:ws:{ws.id} -->` and updated in
        # place by `_find_or_create_comment`. That means a stale run's
        # transition (typically the supersede-cancel that fires when a
        # force-push creates a fresh run for the new HEAD SHA) can land
        # AFTER the fresh run's writes and clobber the comment with the
        # old run's status. The per-SHA commit status check is unaffected
        # (GitHub only surfaces the head SHA's checks), but the comment
        # is the only shared surface, so only the latest run for this PR
        # may write to it.
        if run.vcs_pull_request_number:
            latest_run_id = (
                await db.execute(
                    select(Run.id)
                    .where(
                        Run.workspace_id == ws.id,
                        Run.vcs_pull_request_number == run.vcs_pull_request_number,
                    )
                    .order_by(Run.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest_run_id is not None and latest_run_id != run.id:
                logger.debug(
                    "Skipping PR comment write for superseded run",
                    run_id=run_id_str,
                    latest_run_id=str(latest_run_id),
                    pr=run.vcs_pull_request_number,
                )
                return

            run_url = target_url or f"run-{run.id}"
            # AI plan summary (#401) — included in the comment body when
            # a ready row exists for this run. The summariser re-enqueues
            # this same trigger after a ready row lands so the comment
            # picks up the LLM content as soon as it's available, not on
            # the next status change.
            ai_summary = (
                await db.execute(
                    select(PlanSummary).where(
                        PlanSummary.run_id == run.id,
                        PlanSummary.status == "ready",
                    )
                )
            ).scalar_one_or_none()
            body = _build_comment_body(
                workspace_name=ws.name,
                workspace_id=str(ws.id),
                run_id=f"run-{run.id}",
                run_status=target_status,
                plan_only=run.plan_only,
                has_changes=has_changes,
                run_url=run_url,
                ai_summary=ai_summary,
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
