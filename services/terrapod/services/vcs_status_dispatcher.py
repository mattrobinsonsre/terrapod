"""Triggered task handler for VCS commit status and PR comment posting.

Registered with the distributed scheduler as a trigger handler.
Receives {run_id, workspace_id, target_status} payloads and posts
commit statuses and PR/MR comments back to the VCS provider.
"""

import uuid

from terrapod.db.models import Run, VCSConnection, Workspace
from terrapod.db.session import get_db_session
from terrapod.logging_config import get_logger
from terrapod.services import github_service, gitlab_service, run_links, vcs_status_comment

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

# Terminal run states. A run cannot leave one, so a live status in this set is
# strictly newer than any non-terminal status a trigger payload carries — which
# is what lets a late writer be corrected rather than trusted (#1372).
_TERMINAL_STATUSES = frozenset({"applied", "errored", "discarded", "canceled"})

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


# A run held at a post-plan gate stays in `planning`, so the status map alone
# reported "Plan in progress" for as long as it was held — which is to say
# indefinitely, because nothing moves until a person acts (#1798). The plan has
# finished; what is outstanding is a decision. Each gate names itself and says
# what would release it, because "blocked" without the verb leaves the reader
# hunting through the UI for a button.
_GATE_DESCRIPTION: dict[str, str] = {
    "run-task": "Blocked by a run task — waiting on it, or override/discard the run",
    "policy": "Blocked by a policy check — override or discard the run",
    "security-scan": "Blocked by the security scan — override or discard the run",
    "ai-policy": "Blocked by the AI policy gate — override or discard the run",
}


def _resolve_status(
    run_status: str,
    plan_only: bool,
    has_changes: bool | None = None,
    gate: str | None = None,
) -> tuple[str, str, str]:
    """Map run status to (github_state, gitlab_state, description).

    `gate` is the post-plan gate holding the run, from `run_service.blocked_by`.
    A held run reports `pending` rather than `failure`: the plan succeeded and
    the run is awaiting a decision, which is the same reading the existing
    "awaiting confirmation" case gets. It still keeps a required check unmet,
    so a PR cannot merge past a gate that is holding its run.

    Plan-only 'planned' runs use the plan's has_changes flag to produce a
    descriptive message ("Has changes" / "No changes") instead of the generic
    "Plan finished". The check still reports success in both cases — only the
    text differs. Non-plan-only 'planned' runs keep the awaiting-confirmation
    pending state, annotated with has-changes when known. When has_changes
    is None (older runs, pre-plan statuses) the description falls back to
    the bare form.
    """
    if gate and run_status == "planning":
        return ("pending", "running", _GATE_DESCRIPTION.get(gate, f"Blocked by {gate}"))
    if run_status == "applied" and has_changes is False:
        # A zero-change run short-circuits straight to `applied` without
        # launching an apply — deliberately, because there is nothing to apply
        # and an empty apply trips the duplicate-serial 500 on state upload.
        # The run status is right; "Apply complete" was not (#1794). It read as
        # though something had been applied, which on a workspace with
        # auto-apply OFF is alarming rather than merely imprecise.
        return ("success", "success", "No changes — nothing to apply")
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
        # The payload's status is normally authoritative: it is snapshotted at
        # the transition so the dispatcher does not depend on the DB commit
        # having landed yet. The exception is a payload that has gone STALE —
        # a writer that enqueued before a long side task (the AI summary) and
        # fired after the run finished. Since a terminal state cannot be left,
        # a terminal live status is strictly newer, so prefer it. Without this
        # a failed plan's comment read "Plan in progress" above its own failure
        # analysis (#1372). The comment is a shared, last-write-wins surface,
        # which is why it needs the same kind of guard as the superseded-run
        # check further down.
        if run.status in _TERMINAL_STATUSES and target_status not in _TERMINAL_STATUSES:
            logger.info(
                "Preferring terminal run status over stale trigger payload",
                run_id=run_id_str,
                payload_status=target_status,
                live_status=run.status,
            )
            target_status = run.status
            if payload_has_changes is _UNSET:
                has_changes = run.has_changes

        # Which gate, if any, is holding this run (#1798). Resolved from the
        # live row rather than the payload: a gate blocks AFTER the transition
        # that enqueued this, so the payload cannot know. Best-effort — a
        # status update is not worth failing over a gate lookup.
        gate: str | None = None
        if target_status == "planning":
            # Local import: run_service pulls in the run/engine stack, and this
            # dispatcher is reached from the scheduler on every status change.
            from terrapod.services import run_service

            try:
                gate = await run_service.blocked_by(db, run)
            except Exception as e:
                logger.warning(
                    "Could not resolve the gate holding this run",
                    run_id=run_id_str,
                    error=str(e),
                )

        github_state, gitlab_state, description = _resolve_status(
            target_status, run.plan_only, has_changes, gate
        )

        # Build target URL. "" rather than None because the provider clients
        # take a plain string for the commit status's target.
        target_url = run_links.run_url(ws.id, run.id) or ""

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

        # The PR comment is no longer this module's to write (#1940).
        #
        # It used to post one per workspace, identified by
        # `<!-- terrapod:ws:{ws.id}:{sha} -->` — the commit SHA included, so
        # every push created a fresh comment instead of editing the last one
        # (#1799, which asked for an *option* and got it unconditionally).
        # With the status table doing the same job for every workspace at
        # once, that was the same information twice and the thread grew as
        # workspaces x pushes: a four-workspace PR with four pushes carried
        # seventeen Terrapod comments.
        #
        # So the narrative moved into the table's own per-workspace block and
        # this path just asks for a refresh. One comment per PR, edited.
        #
        # The superseded-run guard that used to stand here is gone with it,
        # and deliberately: it existed because that comment was keyed per
        # (workspace, PR) and a stale run's late transition could clobber a
        # fresher run's status. The table is rendered from the latest run per
        # workspace every time, so a late refresh re-reads current rows and
        # cannot write a stale one.
        if run.vcs_pull_request_number:
            await vcs_status_comment.refresh_for_run(db, run, "status")
    logger.info(
        "VCS commit status posted",
        run_id=run_id_str,
        status=target_status,
        workspace=ws.name if ws else workspace_id_str,
    )
