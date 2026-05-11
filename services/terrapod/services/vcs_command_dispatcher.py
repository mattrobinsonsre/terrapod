"""Dispatcher for `terrapod ...` comments on PRs/MRs (#282 phase 4).

Receives parsed commands from the webhook receiver (or the poll-fallback
comment scanner) and routes them to the right action. Authorization is
delegated to VCS repo permissions (the apply-then-merge contract) — this
module records the VCS actor on whatever run/action it kicks off, but
does not consult Terrapod RBAC.

Triggered task name: `vcs_comment_dispatch`.

Payload shape:
  {
    "connection_id": "<uuid>",        # VCSConnection.id
    "repo": "owner/name",
    "pr_number": 123,
    "comment_id": "987654321",        # provider-side, for dedup / audit
    "actor_login": "octocat",
    "actor_user_id": "12345",
    "body": "terrapod apply -W foo",
  }
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from terrapod.db.models import PRSession, Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import run_service
from terrapod.services.audit_service import log_vcs_action
from terrapod.services.scheduler import enqueue_trigger
from terrapod.services.vcs_command_parser import Command, parse

logger = get_logger(__name__)


# Verbs we route in phase 4. Surfaces that depend on later phases
# (status comment posting, auto-merge) are stubbed with audit-only
# acknowledgements that say "this will work after phase N".
_ROUTABLE_VERBS = frozenset({"plan", "apply", "unlock", "merge", "help"})


async def handle_vcs_comment_dispatch(payload: dict[str, Any]) -> None:
    """Scheduler trigger handler.

    Parses the comment, validates against the PRSession + workspace
    state, and enqueues the appropriate action. Idempotent — the
    dedup key (set by the webhook receiver) prevents duplicate
    dispatches from a webhook/poll race.
    """
    body = payload.get("body") or ""
    cmd = parse(body)
    if cmd is None:
        return  # not a command

    connection_id = payload.get("connection_id")
    repo = payload.get("repo")
    pr_number = payload.get("pr_number")
    if not (connection_id and repo and pr_number):
        logger.warning("vcs_comment_dispatch missing required fields", payload_keys=list(payload))
        return

    actor_login = payload.get("actor_login") or ""
    actor_user_id = str(payload.get("actor_user_id") or "")

    async with get_db_session() as db:
        conn = await db.get(VCSConnection, uuid.UUID(connection_id))
        if conn is None:
            logger.warning("vcs_comment_dispatch: unknown connection", connection_id=connection_id)
            return

        sess_result = await db.execute(
            select(PRSession).where(
                PRSession.vcs_connection_id == conn.id,
                PRSession.repo == repo,
                PRSession.pr_number == pr_number,
            )
        )
        sess = sess_result.scalar_one_or_none()
        # No active session means either (a) no apply-then-merge
        # workspace plans against this PR, or (b) the PR closed before
        # the dispatcher ran. Silently drop — there's nothing to act on.
        if sess is None or sess.state != "open":
            logger.info(
                "vcs_comment_dispatch: no open session for PR",
                connection_id=connection_id,
                repo=repo,
                pr_number=pr_number,
                verb=cmd.verb,
            )
            return

        # Find PR-affected apply-then-merge workspaces (the ones the
        # commands actually operate on). Other workspaces (different
        # mode, different repo) are ignored.
        ws_result = await db.execute(
            select(Workspace).where(
                Workspace.vcs_connection_id == conn.id,
                Workspace.vcs_workflow == "apply_then_merge",
            )
        )
        candidates = [
            ws
            for ws in ws_result.scalars().all()
            if (ws.vcs_repo_url or "").rstrip("/").endswith(repo)
        ]
        if cmd.workspace:
            candidates = [ws for ws in candidates if ws.name == cmd.workspace]

        await _route(db, cmd, conn, sess, candidates, actor_login, actor_user_id)


async def _route(
    db,
    cmd: Command,
    conn: VCSConnection,
    sess: PRSession,
    candidates: list[Workspace],
    actor_login: str,
    actor_user_id: str,
) -> None:
    """Dispatch a parsed command to the right action."""
    audit_ctx = {
        "verb": cmd.verb,
        "repo": sess.repo,
        "pr_number": sess.pr_number,
        "actor_login": actor_login,
        "actor_user_id": actor_user_id,
        "candidate_count": len(candidates),
    }

    if cmd.verb == "help":
        # Help-back is delivered via the status-comment surface
        # (phase 6). For now we audit-log so the dispatch is visible.
        logger.info("vcs_comment_dispatch: help requested", **audit_ctx)
        return

    if cmd.verb == "merge":
        # Force-merge: skip the cross-workspace gate, record the partial
        # apply state at merge time in the audit log, then call the
        # provider's merge API.
        from terrapod.services.vcs_auto_merge import force_merge

        merged, error_reason = await force_merge(
            db, sess, conn, "merge", actor_login, actor_user_id
        )
        if merged:
            logger.info(
                "vcs_comment_dispatch: force-merged",
                **audit_ctx,
                strategy="merge",
            )
        else:
            logger.info(
                "vcs_comment_dispatch: force-merge rejected by provider",
                **audit_ctx,
                error_reason=error_reason,
            )
        # Refresh the status comment so the merge result is visible.
        await db.commit()
        await enqueue_trigger(
            "vcs_status_comment_update",
            {"session_id": str(sess.id)},
            dedup_key=f"vcs_status:{sess.id}",
        )
        return

    if not candidates:
        if cmd.workspace:
            logger.info(
                "vcs_comment_dispatch: workspace not affected by PR",
                workspace_filter=cmd.workspace,
                **audit_ctx,
            )
        else:
            logger.info("vcs_comment_dispatch: no apply-then-merge workspaces on PR", **audit_ctx)
        return

    if cmd.verb == "apply":
        await _route_apply(db, sess, candidates, actor_login, actor_user_id)
    elif cmd.verb == "plan":
        await _route_plan(db, sess, candidates, actor_login, actor_user_id)
    elif cmd.verb == "unlock":
        await _route_unlock(db, candidates, actor_login, actor_user_id)

    # Audit: one entry per affected workspace. Dual-actor model — see
    # log_vcs_action / #282. Errors swallowed so audit failure never
    # breaks the dispatch path.
    for ws in candidates:
        try:
            await log_vcs_action(
                db,
                verb=cmd.verb,
                workspace_id=str(ws.id),
                actor_login=actor_login,
                actor_user_id=actor_user_id,
                pr_number=sess.pr_number,
                repo=sess.repo,
                detail=cmd.raw,
            )
        except Exception as e:
            logger.warning("audit log failed", verb=cmd.verb, error=str(e))

    # Every command-driven mutation refreshes the status comment so the
    # PR thread reflects the latest state in seconds.
    await enqueue_trigger(
        "vcs_status_comment_update",
        {"session_id": str(sess.id)},
        dedup_key=f"vcs_status:{sess.id}",
    )


async def _route_apply(
    db,
    sess: PRSession,
    candidates: list[Workspace],
    actor_login: str,
    actor_user_id: str,
) -> None:
    """For each candidate workspace, confirm the current planned run.

    The mergeability gate is enforced inside `run_service.confirm_run`
    (phase 5) — if the PR isn't mergeable, confirm raises and we log /
    later surface the reason on the status comment.
    """
    for ws in candidates:
        # Find the most recent planned run for this PR on this workspace.
        result = await db.execute(
            select(Run)
            .where(
                Run.workspace_id == ws.id,
                Run.vcs_pull_request_number == sess.pr_number,
                Run.status == "planned",
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        run = result.scalar_one_or_none()
        if run is None:
            logger.info(
                "apply: no planned run for workspace",
                workspace=ws.name,
                pr_number=sess.pr_number,
            )
            continue
        # Stamp the actor before triggering confirm so the audit trail
        # captures who applied the run from the comment side.
        run.vcs_actor_login = actor_login
        run.vcs_actor_user_id = actor_user_id
        try:
            await run_service.confirm_run(db, run)
            logger.info(
                "apply: confirmed",
                workspace=ws.name,
                run_id=str(run.id),
                pr_number=sess.pr_number,
                actor_login=actor_login,
            )
        except run_service.ApplyBlocked as e:
            # Mergeability gate rejected. `vcs_apply_blocked_reason` is
            # already persisted on the run by the gate — that's what the
            # status comment (phase 6) reads to render the block message.
            logger.info(
                "apply: blocked by mergeability gate",
                workspace=ws.name,
                run_id=str(run.id),
                pr_number=sess.pr_number,
                reason=e.reason,
            )
        except Exception as e:
            logger.warning(
                "apply: confirm_run failed",
                workspace=ws.name,
                run_id=str(run.id),
                error=str(e),
            )
    await db.commit()


async def _route_plan(
    db,
    sess: PRSession,
    candidates: list[Workspace],
    actor_login: str,
    actor_user_id: str,
) -> None:
    """Discard the current run on each candidate and let the poller
    create a fresh one against the same head SHA.

    Cancelling here releases the workspace lock; the next poll cycle's
    "new PR head SHA" detection notices that this PR has no current
    run for its head SHA and creates one. We don't create the run
    inline because doing so duplicates the poller's archive-fetch +
    config-version-upload path; let the existing machinery do its job.
    """
    for ws in candidates:
        active = await db.execute(
            select(Run).where(
                Run.workspace_id == ws.id,
                Run.vcs_pull_request_number == sess.pr_number,
                Run.status.notin_(run_service.TERMINAL_STATES),
            )
        )
        for run in active.scalars().all():
            run.vcs_actor_login = actor_login
            run.vcs_actor_user_id = actor_user_id
            try:
                await run_service.cancel_run(db, run, force=True)
                logger.info(
                    "plan: cancelled existing run",
                    workspace=ws.name,
                    run_id=str(run.id),
                    pr_number=sess.pr_number,
                )
            except Exception as e:
                logger.warning(
                    "plan: cancel_run failed",
                    workspace=ws.name,
                    run_id=str(run.id),
                    error=str(e),
                )
    await db.commit()


async def _route_unlock(
    db,
    candidates: list[Workspace],
    actor_login: str,
    actor_user_id: str,
) -> None:
    """Release the workspace lock if stuck.

    Unlock is a manual escape hatch (Atlantis ships the same). Per the
    authorization model, anyone who can comment on the PR can unlock —
    branch protection isn't a meaningful gate here because no apply is
    happening.
    """
    for ws in candidates:
        if ws.locked:
            ws.locked = False
            ws.lock_id = None
            logger.info(
                "unlock: released workspace lock",
                workspace=ws.name,
                actor_login=actor_login,
            )
    await db.commit()
