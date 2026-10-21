"""Cross-workspace auto-merge gate + executor (#282 phase 8).

When a PR-associated apply completes, this handler evaluates whether the
PR is ready to merge — every PR-affected workspace must have met its
per-mode required state for the current head SHA:

- `apply_then_merge` workspaces: have an `applied` run for the head SHA
  (or `has_changes=False`, which auto-counts as applied).
- `merge_then_apply` workspaces: have a `planned` speculative run for
  the head SHA (i.e. the speculative plan succeeded, regardless of
  has_changes).

If all PR-affected workspaces meet their bar AND at least one has
`auto_merge=true`, fire the VCS merge API.

This is the ONLY route from Terrapod to a provider merge, and it is
deliberately narrow: it fires from a completed apply, under a workspace
setting an operator wrote, once every affected workspace has met its gate.
There is no comment-driven force-merge -- `terrapod merge` used to bypass
this gate on nothing but the fact that someone could type a comment, and was
removed. A merge that should happen despite an incomplete apply is made on
the provider, by someone the repository trusts to merge.

Triggered task: `vcs_apply_completed`. Payload:
  { "run_id": "<uuid>", "workspace_id": "<uuid>", "pr_number": <int> }
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from terrapod.db.models import PRSession, Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, vcs_provider
from terrapod.services.scheduler import enqueue_trigger

logger = get_logger(__name__)


def _meets_required_state(ws: Workspace, run: Run | None) -> bool:
    """Per-mode gate: is this workspace's latest run for the head SHA OK?"""
    if run is None:
        return False
    if ws.vcs_workflow == "apply_then_merge":
        # Apply succeeded, OR plan reported no changes (state-equivalent).
        return run.status == "applied" or (run.status == "planned" and run.has_changes is False)
    # merge_then_apply: speculative plan must have succeeded. Plan-only
    # speculative runs terminate at `planned`.
    return run.status == "planned"


async def _latest_run_for_pr(
    db, workspace_id: uuid.UUID, pr_number: int, head_sha: str
) -> Run | None:
    """Return the latest run on this workspace for (pr, head_sha)."""
    result = await db.execute(
        select(Run)
        .where(
            Run.workspace_id == workspace_id,
            Run.vcs_pull_request_number == pr_number,
            Run.vcs_commit_sha == head_sha,
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _affected_workspaces(db, sess: PRSession) -> list[Workspace]:
    """Workspaces with runs for this PR on this connection+repo."""
    result = await db.execute(
        select(Workspace)
        .join(Run, Run.workspace_id == Workspace.id)
        .where(
            Run.vcs_pull_request_number == sess.pr_number,
            Workspace.vcs_connection_id == sess.vcs_connection_id,
            Workspace.vcs_repo_url.endswith(sess.repo),
        )
        .distinct()
    )
    return list(result.scalars().all())


async def handle_vcs_apply_completed(payload: dict[str, Any]) -> None:
    """Triggered handler: re-evaluate the merge gate after a PR-run apply.

    Doesn't unconditionally merge — only fires if at least one
    PR-affected workspace has `auto_merge=true` AND every PR-affected
    workspace meets its per-mode required state for the current head
    SHA. The status comment is refreshed regardless so the user can see
    progress.
    """
    pr_number = payload.get("pr_number")
    workspace_id_str = payload.get("workspace_id")
    if pr_number is None or workspace_id_str is None:
        return
    async with get_db_session() as db:
        ws = await db.get(Workspace, uuid.UUID(workspace_id_str))
        if ws is None or ws.vcs_connection_id is None or ws.vcs_connection is None:
            return
        # The repo is part of `uq_pr_session` and was missing here, exactly as
        # it was in `vcs_status_comment.refresh_for_run` (#2199) -- one
        # connection is one app installation across a whole organisation, so
        # `(connection, pr_number)` matches every same-numbered open PR in it.
        # This call site used `scalar_one_or_none`, so the symptom was the
        # opposite of the comment one and worse: two such PRs raised
        # `MultipleResultsFound`, the scheduler logged it and counted it, and
        # auto-merge simply never fired for either of them.
        #
        # `_affected_workspaces` below has always scoped by repo, so once the
        # session is right the rest of this handler was never wrong.
        repo = vcs_provider.repo_path(ws.vcs_connection, ws.vcs_repo_url)
        if repo is None:
            # Same refusal as the status-comment path, and said out loud for the
            # same reason: not merging is the safe answer, but not merging in
            # silence is how this class of bug survives.
            logger.warning(
                "Cannot resolve the PR session: unparseable workspace repo URL",
                workspace_id=str(ws.id),
                repo_url=ws.vcs_repo_url,
                provider=ws.vcs_connection.provider,
            )
            return
        sess_result = await db.execute(
            select(PRSession).where(
                PRSession.vcs_connection_id == ws.vcs_connection_id,
                PRSession.repo == repo,
                PRSession.pr_number == pr_number,
                # `open` only, not `_LIVE_SESSION_STATES`: this is about to
                # merge, and a session already recorded as merged must not be
                # merged again.
                PRSession.state == "open",
            )
        )
        sess = sess_result.scalar_one_or_none()
        if sess is None:
            return

        # Refresh the status comment unconditionally so the apply
        # outcome shows up even if auto-merge doesn't fire.
        await enqueue_trigger(
            "vcs_status_comment_update",
            {"session_id": str(sess.id)},
            dedup_key=f"vcs_status:{sess.id}",
        )

        affected = await _affected_workspaces(db, sess)
        if not affected:
            return
        any_auto_merge = any(w.auto_merge for w in affected)
        if not any_auto_merge:
            # No workspace opted into auto-merge — even if the gate is
            # green, merging stays a decision someone makes on the provider.
            return

        # Evaluate the gate.
        for w in affected:
            run = await _latest_run_for_pr(db, w.id, pr_number, sess.head_sha)
            if not _meets_required_state(w, run):
                logger.info(
                    "auto_merge gate not met",
                    workspace=w.name,
                    pr_number=pr_number,
                    run_status=(run.status if run else None),
                )
                return  # gate not met; let the next state change re-evaluate

        # Gate green — execute the merge.
        conn = await db.get(VCSConnection, ws.vcs_connection_id)
        if conn is None:
            return
        # Strategy: prefer the policy of any auto_merge=true workspace.
        # When multiple workspaces disagree, use the first one's setting
        # — the user opted in, they chose the strategy. Document.
        strategy = next((w.auto_merge_strategy for w in affected if w.auto_merge), "merge")
        await _execute_merge(conn, sess, strategy)
        await db.commit()


async def _execute_merge(conn: VCSConnection, sess: PRSession, strategy: str) -> None:
    """Provider-dispatched merge."""
    owner, repo = sess.repo.split("/", 1)
    if conn.provider == "github":
        merge_fn = github_service.merge_pull_request
    elif conn.provider == "gitlab":
        merge_fn = gitlab_service.merge_pull_request
    else:
        logger.warning("auto_merge: unknown provider", provider=conn.provider)
        return
    try:
        result = await merge_fn(conn, owner, repo, sess.pr_number, strategy)
    except Exception as e:
        logger.warning(
            "auto_merge: provider call raised",
            pr_number=sess.pr_number,
            error=str(e),
        )
        return
    if result.merged:
        sess.state = "merged"
        logger.info(
            "auto_merge: PR merged",
            pr_number=sess.pr_number,
            sha=result.sha,
            strategy=strategy,
        )
    else:
        logger.info(
            "auto_merge: provider rejected",
            pr_number=sess.pr_number,
            reason=result.error_reason,
        )
