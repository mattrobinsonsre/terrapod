"""Renders + posts the per-PR status comment (#282 phase 6).

One Terrapod-authored comment per PR/MR, edited in place. The renderer
collects every PR-affected workspace across modes (apply_then_merge and
merge_then_apply) and emits a single Markdown table per the worked
examples in #282.

Posted-from triggers:
  - vcs_poller after a plan finishes for a PR run
  - run_service apply-completion path
  - vcs_command_dispatcher on every command
  - run_reconciler when a planned run is invalidated by a sibling apply

The triggered task handler is `vcs_status_comment_update`. It's
idempotent — the same payload run multiple times converges on the same
comment body.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from terrapod.db.models import PRSession, Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service

logger = get_logger(__name__)


# Marker hidden in the comment body so we can find our own comment on a
# PR even if the status_comment_id isn't recorded (e.g. comment created
# manually, or PRSession was rebuilt). The HTML-comment form is invisible
# to GitHub / GitLab rendering.
_COMMENT_MARKER = "<!-- terrapod:status-comment -->"


@dataclass(frozen=True)
class _Row:
    """One row in the rendered status table."""

    workspace_name: str
    mode: str
    plan_summary: str
    apply_summary: str
    mergeable_summary: str


def _plan_summary(run: Run | None) -> str:
    """Compact plan summary: `+ N ~ N` style, or status word for non-planned."""
    if run is None:
        return "—"
    if run.status in ("pending", "queued"):
        return "queued"
    if run.status == "planning":
        return "running"
    if run.status == "errored":
        return "errored"
    if run.status == "discarded":
        return "discarded"
    if run.status == "canceled":
        return "canceled"
    # Planned / applying / applied — we don't currently parse the plan
    # log to extract add/change/destroy counts. has_changes carries the
    # bool. Refine later by surfacing the counts from `plan-result` or
    # `tofu show -json`.
    if run.has_changes is False:
        return "no changes"
    return "changes"


def _apply_summary(run: Run | None) -> str:
    if run is None:
        return "—"
    if run.status == "applied":
        return "applied"
    if run.status == "applying":
        return "applying"
    if run.status == "errored":
        return "errored"
    if run.status == "discarded":
        return "discarded"
    if run.status == "canceled":
        return "canceled"
    return "not applied"


def _mergeable_summary(run: Run | None) -> str:
    if run is None:
        return "—"
    if run.vcs_apply_blocked_reason:
        return f"blocked: {run.vcs_apply_blocked_reason[:60]}"
    return "yes"


def render_comment(rows: list[_Row], *, force_merge_hint: bool = False) -> str:
    """Render the Markdown status-comment body.

    Mode-aware rows: apply_then_merge workspaces show 'not applied' /
    'applied'; merge_then_apply workspaces show 'will apply on merge'
    in the apply column because they don't apply pre-merge by design.
    """
    if not rows:
        return f"{_COMMENT_MARKER}\n\n_No Terrapod workspaces affected by this PR._"

    header = "| Workspace | Mode | Plan | Apply | Mergeable |\n|---|---|---|---|---|"
    body_lines: list[str] = []
    pending_apply: list[str] = []
    for r in rows:
        # merge_then_apply rows annotate the apply column to make the
        # mode-distinction explicit; apply_then_merge rows pass through.
        apply_cell = "will apply on merge" if r.mode == "merge_then_apply" else r.apply_summary
        body_lines.append(
            f"| `{_escape(r.workspace_name)}` | {r.mode} | {r.plan_summary} | {apply_cell} | {r.mergeable_summary} |"
        )
        if r.mode == "apply_then_merge" and r.apply_summary == "not applied":
            pending_apply.append(r.workspace_name)

    parts: list[str] = [_COMMENT_MARKER, "", header, *body_lines]
    if pending_apply:
        if len(pending_apply) == 1:
            parts.append("")
            parts.append(f"Comment `terrapod apply` to apply `{_escape(pending_apply[0])}`.")
        else:
            parts.append("")
            parts.append(
                "Comment `terrapod apply` to apply all pending workspaces, "
                "or `terrapod apply -W <workspace>` for one at a time."
            )
    if force_merge_hint:
        parts.append("")
        parts.append(
            "Auto-merge is blocked. Use `terrapod merge` to merge despite incomplete applies."
        )
    return "\n".join(parts)


def _escape(text: str) -> str:
    """Minimal Markdown / table-cell escaping for user-controlled cells."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


async def _collect_rows(db, sess: PRSession) -> list[_Row]:
    """Find every workspace whose runs reference this PR, latest run per."""
    # Workspaces in either mode that have a run for this PR. We don't
    # have a direct (connection, repo, pr) → workspace index, so we
    # pivot through the Run rows themselves.
    result = await db.execute(
        select(Run, Workspace)
        .join(Workspace, Workspace.id == Run.workspace_id)
        .where(
            Run.vcs_pull_request_number == sess.pr_number,
            Workspace.vcs_connection_id == sess.vcs_connection_id,
            (Workspace.vcs_repo_url.endswith(sess.repo)),
        )
        .order_by(Workspace.name, Run.created_at.desc())
    )
    # Reduce to latest run per workspace.
    latest_per_ws: dict[uuid.UUID, tuple[Workspace, Run]] = {}
    for run, ws in result.all():
        latest_per_ws.setdefault(ws.id, (ws, run))

    rows: list[_Row] = []
    for ws, run in sorted(latest_per_ws.values(), key=lambda pair: pair[0].name):
        rows.append(
            _Row(
                workspace_name=ws.name,
                mode=ws.vcs_workflow,
                plan_summary=_plan_summary(run),
                apply_summary=_apply_summary(run),
                mergeable_summary=_mergeable_summary(run),
            )
        )
    return rows


async def _post_or_update(
    conn: VCSConnection,
    repo: str,
    pr_number: int,
    sess: PRSession,
    body: str,
) -> None:
    """Provider-dispatched post-or-update via the existing comment helpers."""
    owner, repo_name = repo.split("/", 1)
    if conn.provider == "github":
        post = github_service.create_pr_comment
        update = github_service.update_pr_comment
    elif conn.provider == "gitlab":
        post = gitlab_service.create_mr_comment
        update = gitlab_service.update_mr_comment
    else:
        logger.warning("status comment: unknown provider", provider=conn.provider)
        return

    try:
        if sess.status_comment_id:
            if conn.provider == "gitlab":
                # GitLab update takes (conn, owner, repo, mr_number, note_id, body)
                await update(conn, owner, repo_name, pr_number, int(sess.status_comment_id), body)
            else:
                await update(conn, owner, repo_name, int(sess.status_comment_id), body)
            return
        new_id = await post(conn, owner, repo_name, pr_number, body)
        sess.status_comment_id = str(new_id)
    except Exception as e:
        # Status comment failure must never break the run lifecycle. Log
        # and move on; the next state-change trigger will retry.
        logger.warning(
            "status comment post/update failed",
            provider=conn.provider,
            pr_number=pr_number,
            error=str(e),
        )


async def handle_vcs_status_comment_update(payload: dict[str, Any]) -> None:
    """Scheduler trigger handler.

    Payload:
      { "session_id": "<uuid>" }
    """
    session_id = payload.get("session_id")
    if not session_id:
        return
    async with get_db_session() as db:
        sess = await db.get(PRSession, uuid.UUID(session_id))
        if sess is None or sess.state != "open":
            return
        conn = await db.get(VCSConnection, sess.vcs_connection_id)
        if conn is None:
            return
        rows = await _collect_rows(db, sess)
        body = render_comment(rows)
        await _post_or_update(conn, sess.repo, sess.pr_number, sess, body)
        await db.commit()
