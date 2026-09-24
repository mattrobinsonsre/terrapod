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

import re
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

# Kept in step with docs/vcs-workflows.md, which documents the same table.
_HELP_BODY = "\n".join(
    [
        "### Terrapod commands",
        "",
        "| Command | What it does |",
        "|---|---|",
        "| `terrapod plan` | Re-plan every workspace this PR affects |",
        "| `terrapod apply` | Apply the current planned run for every affected workspace |",
        "| `terrapod apply -W <workspace>` | Apply a single workspace |",
        "| `terrapod unlock` | Release the workspace lock if it is stuck |",
        "| `terrapod merge` | Force-merge despite incomplete applies (audit-logged) |",
        "| `terrapod help` | This list |",
        "",
        "A command in a code-fenced block is ignored, so quoting one in a "
        "discussion never triggers it.",
    ]
)


# Reasons a command is dropped. Each one answers the question the author is
# actually asking — "did you get it?" — and then says what to do about it.
# Silence cannot distinguish "not received" from "received and ignored", which
# is the complaint #1799 was opened about.
_NO_SESSION_BODY = (
    "Terrapod received this command, but it is not tracking this "
    "pull request, so there is nothing to run.\n\n"
    "That happens when no workspace in **apply-then-merge** mode plans "
    "against this repository and branch, or when the pull request was "
    "closed before the command arrived. Workspaces in the default "
    "merge-then-apply mode do not take commands — they plan on the pull "
    "request and apply after it merges.\n\n"
    "See [VCS workflows](https://github.com/mattrobinsonsre/terrapod/blob/main/docs/vcs-workflows.md)."
)

_NO_CANDIDATES_BODY = (
    "Terrapod received this command, but no **apply-then-merge** workspace "
    "is affected by this pull request, so there is nothing to run.\n\n"
    "Check that a workspace points at this repository and that its "
    "working directory matches a path this pull request changes."
)


def _no_workspace_body(name: str) -> str:
    return (
        f"Terrapod received this command, but no workspace named `{name}` is "
        "affected by this pull request.\n\n"
        "Check the spelling, and that the workspace is in **apply-then-merge** "
        "mode with a working directory this pull request touches. Omit "
        "`-W` to act on every affected workspace."
    )


#: A flag, as every documented command's arguments are (`-W <workspace>`).
_FLAG = re.compile(r"^-{1,2}[A-Za-z]")


def _looks_like_a_command_attempt(raw: str) -> bool:
    """Whether an unrecognised line was plausibly aimed at Terrapod.

    The parser maps every unknown verb to `help`, so without this a comment
    that merely BEGINS with the word -- "terrapod is working well now" -- drew
    a twelve-line usage table onto someone's pull request, unprompted and with
    no way to switch it off (#1836).

    The test is: the verb stands alone, or is followed by a FLAG. That is what
    every documented command looks like (`terrapod apply -W web`), and it is
    the only signal that separates a typo from prose -- counting trailing
    tokens does not, because "working well now" is three perfectly
    workspace-shaped words.

    Deliberately biased toward silence. A missed typo hint costs the author one
    puzzled moment; an unsolicited table on every passing mention is noise
    every reviewer on that PR has to scroll past, and there is no opt-out.
    """
    parts = raw.split()
    if len(parts) < 2:
        return False
    rest = parts[2:]
    return not rest or bool(_FLAG.match(rest[0]))


def _unrecognised_verb(raw: str) -> str | None:
    """The verb from a line the parser could not route, or None.

    Read back off `raw` rather than taken from `Command.unrecognised`, which
    is set only when there is NO trailing text -- so `terrapod aply -W web`,
    the most command-shaped typo there is, arrived with it empty and got the
    generic table instead of its own name back.
    """
    parts = raw.split()
    if len(parts) < 2:
        return None
    verb = parts[1].strip(".,!?:;").lower()
    return None if verb == "help" else verb


def _unknown_verb_body(verb: str) -> str:
    return f"Terrapod does not recognise `{verb}`.\n\n{_HELP_BODY}"


# The acknowledgement emoji. Spelled without colons, which is what both
# GitHub's reactions API and GitLab's award-emoji API expect.
ACK_RECEIVED = "eyes"
ACK_DONE = "thumbsup"
ACK_REJECTED = "thumbsdown"


async def _post_comment(conn: VCSConnection, repo: str, pr_number: int, body: str) -> None:
    """Post a standalone comment on the PR/MR, addressed by (repo, number).

    Deliberately a NEW comment rather than an edit of the status comment: a
    reply to something you just typed belongs next to it, and the status
    comment is edited in place, often far up the thread.

    Takes the coordinates rather than a `PRSession` because the cases that
    most need an answer are the ones with no session to read them from
    (#1799) — a command on a PR Terrapod is not tracking is exactly the
    silence the issue is about.

    Best-effort. A reply that cannot be posted must never fail the dispatch —
    the command it accompanies has already run.
    """
    from terrapod.services import github_service, gitlab_service

    try:
        # `repo` is "owner/name" throughout this path — `PRSession.repo` is
        # stored that way and the webhook passes GitHub's `full_name`.
        owner, _, repo_name = repo.partition("/")
        if not repo_name:
            logger.warning("vcs_comment_dispatch: malformed repo", repo=repo)
            return
        if conn.provider == "gitlab":
            await gitlab_service.create_mr_comment(conn, owner, repo_name, pr_number, body)
        else:
            await github_service.create_pr_comment(conn, owner, repo_name, pr_number, body)
    except Exception as e:
        logger.warning(
            "vcs_comment_dispatch: could not post reply",
            repo=repo,
            pr_number=pr_number,
            error=str(e),
        )


async def _post_reply(db, sess: PRSession, body: str) -> None:
    """Post a standalone comment on the PR/MR this session tracks."""
    conn = await db.get(VCSConnection, sess.vcs_connection_id)
    if conn is None:
        return
    await _post_comment(conn, sess.repo, sess.pr_number, body)


async def _react(
    conn: VCSConnection, repo: str, pr_number: int, comment_id: str, content: str
) -> int | None:
    """React to the command comment. Returns the reaction id, or None.

    Best-effort for the same reason the reply is, and for one more: an
    acknowledgement that fails must never be the thing that stops a command
    from running. A deployment whose App predates the permission simply
    gets no emoji, and every other signal — the reply, the status comment,
    the commit status — is unaffected.
    """
    from terrapod.services import github_service, gitlab_service

    try:
        owner, _, repo_name = repo.partition("/")
        if not repo_name:
            return None
        if conn.provider == "gitlab":
            return await gitlab_service.add_comment_reaction(
                conn, owner, repo_name, pr_number, int(comment_id), content
            )
        return await github_service.add_comment_reaction(
            conn, owner, repo_name, int(comment_id), content
        )
    except Exception as e:
        logger.info(
            "vcs_comment_dispatch: could not react to comment",
            repo=repo,
            pr_number=pr_number,
            comment_id=comment_id,
            content=content,
            error=str(e),
        )
        return None


async def _unreact(
    conn: VCSConnection, repo: str, pr_number: int, comment_id: str, reaction_id: int
) -> None:
    """Remove a reaction we added. Best-effort, as above."""
    from terrapod.services import github_service, gitlab_service

    try:
        owner, _, repo_name = repo.partition("/")
        if not repo_name:
            return
        if conn.provider == "gitlab":
            await gitlab_service.remove_comment_reaction(
                conn, owner, repo_name, pr_number, int(comment_id), reaction_id
            )
        else:
            await github_service.remove_comment_reaction(
                conn, owner, repo_name, int(comment_id), reaction_id
            )
    except Exception as e:
        logger.info(
            "vcs_comment_dispatch: could not remove reaction",
            repo=repo,
            pr_number=pr_number,
            comment_id=comment_id,
            error=str(e),
        )


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
    comment_id = str(payload.get("comment_id") or "")

    async with get_db_session() as db:
        conn = await db.get(VCSConnection, uuid.UUID(connection_id))
        if conn is None:
            logger.warning("vcs_comment_dispatch: unknown connection", connection_id=connection_id)
            return

        # Acknowledge receipt BEFORE any validation, because the cases that
        # most need acknowledging are the ones that go on to drop the
        # command. An eye that is never replaced says "received, outcome
        # unknown", which is the honest state if this process dies here.
        ack = await _react(conn, repo, pr_number, comment_id, ACK_RECEIVED) if comment_id else None

        async def settle(accepted: bool) -> None:
            """Replace the eyes with the outcome."""
            if not comment_id:
                return
            await _react(conn, repo, pr_number, comment_id, ACK_DONE if accepted else ACK_REJECTED)
            if ack is not None:
                await _unreact(conn, repo, pr_number, comment_id, ack)

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
        # the dispatcher ran. There is nothing to act on — but saying so
        # is the difference between "not received" and "received and
        # ignored", which is the whole of #1799.
        if sess is None or sess.state != "open":
            logger.info(
                "vcs_comment_dispatch: no open session for PR",
                connection_id=connection_id,
                repo=repo,
                pr_number=pr_number,
                verb=cmd.verb,
            )
            await _post_comment(conn, repo, pr_number, _NO_SESSION_BODY)
            await settle(False)
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

        accepted = await _route(db, cmd, conn, sess, candidates, actor_login, actor_user_id)
        await settle(accepted)


async def _route(
    db,
    cmd: Command,
    conn: VCSConnection,
    sess: PRSession,
    candidates: list[Workspace],
    actor_login: str,
    actor_user_id: str,
) -> bool:
    """Dispatch a parsed command to the right action.

    Returns whether the command was acted on, which decides the outcome
    reaction the caller leaves on the comment (#1799). "Acted on" means
    routed, not finished: an apply that is queued has been accepted even
    though its run has not started.
    """
    audit_ctx = {
        "verb": cmd.verb,
        "repo": sess.repo,
        "pr_number": sess.pr_number,
        "actor_login": actor_login,
        "actor_user_id": actor_user_id,
        "candidate_count": len(candidates),
    }

    if cmd.verb == "help":
        # This used to audit-log and return, pending a "phase 6" that never
        # arrived (#1797) — so `terrapod help` did nothing visible, and because
        # the parser maps every UNKNOWN verb to `help`, a typo was silent too.
        # That is the worst case: the author cannot tell a mistyped command
        # from one Terrapod never received.
        logger.info(
            "vcs_comment_dispatch: help requested",
            unrecognised=cmd.unrecognised,
            **audit_ctx,
        )
        unrecognised = cmd.unrecognised or _unrecognised_verb(cmd.raw)
        if unrecognised:
            # Only when the line was plausibly aimed at us. A passing mention
            # in prose is not a command, and answering it puts a usage table
            # on someone's PR for nothing (#1836).
            if not _looks_like_a_command_attempt(cmd.raw):
                logger.info(
                    "vcs_comment_dispatch: prose mention, not replying",
                    raw=cmd.raw[:120],
                    **audit_ctx,
                )
                return False
            # Name the token, so a typo reads as a typo rather than as
            # Terrapod volunteering a usage table for no reason (#1799).
            await _post_reply(db, sess, _unknown_verb_body(unrecognised))
            return False
        await _post_reply(db, sess, _HELP_BODY)
        return True

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
        # A merge the provider refused is a rejected command, not a done one
        # — the status comment carries the reason.
        return merged

    if not candidates:
        if cmd.workspace:
            logger.info(
                "vcs_comment_dispatch: workspace not affected by PR",
                workspace_filter=cmd.workspace,
                **audit_ctx,
            )
            await _post_reply(db, sess, _no_workspace_body(cmd.workspace))
        else:
            logger.info("vcs_comment_dispatch: no apply-then-merge workspaces on PR", **audit_ctx)
            await _post_reply(db, sess, _NO_CANDIDATES_BODY)
        return False

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
    return True


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
    """Cancel the current run on each candidate and create a fresh one
    against the same head SHA.

    This used to cancel and leave it to the poller, on the reasoning that
    the poller's "new PR head SHA" detection would notice the PR had no
    current run and make one. It never did (#1795): the poller's dedup
    matches ANY run for the (workspace, sha, pr) triple, terminal ones
    included, so the run this command had just cancelled was itself the
    thing that blocked the replacement. `terrapod plan` cancelled the PR's
    only run and left nothing behind — and because the dedup is keyed on
    the SHA, every later `terrapod plan` on that commit did nothing at
    all. Pushing a commit was the only way out.

    Creating it here is not the duplication the old docstring feared: we
    call the poller's own `_create_vcs_run`, so the archive-fetch and
    config-version-upload path is the same one. What changes is that the
    intent is explicit rather than an inferred side effect.

    The SHA and branch come off the run we just cancelled rather than from
    the provider. The session records the head SHA but not the head REF,
    and a run carries both — so the row we are replacing already knows
    everything needed to replace it, with no extra API call and no schema
    change.
    """
    # Local import: the dispatcher is reached from the scheduler, and the
    # poller pulls in the archive/storage stack. Importing it at module
    # scope would drag that into every dispatch.
    from terrapod.services.vcs_poller import _create_vcs_run, _provider_parse_repo_url

    for ws in candidates:
        active = await db.execute(
            select(Run).where(
                Run.workspace_id == ws.id,
                Run.vcs_pull_request_number == sess.pr_number,
                Run.status.notin_(run_service.TERMINAL_STATES),
            )
        )
        canceled_any = False
        for run in active.scalars().all():
            run.vcs_actor_login = actor_login
            run.vcs_actor_user_id = actor_user_id
            try:
                await run_service.cancel_run(db, run, force=True)
                canceled_any = True
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
        await _replan_pr(
            db,
            ws,
            sess,
            actor_login,
            actor_user_id,
            create=_create_vcs_run,
            parse_repo_url=_provider_parse_repo_url,
            canceled_any=canceled_any,
        )
    await db.commit()


async def _replan_pr(
    db,
    ws: Workspace,
    sess: PRSession,
    actor_login: str,
    actor_user_id: str,
    *,
    create,
    parse_repo_url,
    canceled_any: bool,
) -> None:
    """Create the replacement plan for `terrapod plan` on one workspace.

    Separate from `_route_plan` so the cancel loop stays readable and so a
    failure here cannot stop the remaining candidates being re-planned: a
    command naming several workspaces should not lose the rest because one
    could not be fetched.
    """
    # The newest run for this PR, whatever its state, is where the head ref
    # lives. Without one there is nothing to infer the branch from and
    # nothing that was cancelled either, so there is no re-plan to do —
    # the poller creates the PR's first run.
    prior = await db.execute(
        select(Run)
        .where(Run.workspace_id == ws.id, Run.vcs_pull_request_number == sess.pr_number)
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    previous = prior.scalar_one_or_none()
    if previous is None or not previous.vcs_branch:
        logger.info(
            "plan: no prior run to re-plan from; leaving it to the poller",
            workspace=ws.name,
            pr_number=sess.pr_number,
            canceled_any=canceled_any,
        )
        return

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if conn is None:
        logger.warning("plan: workspace has no VCS connection", workspace=ws.name)
        return
    parsed = parse_repo_url(conn, ws.vcs_repo_url)
    if parsed is None:
        logger.warning(
            "plan: unparseable repo url",
            workspace=ws.name,
            repo_url=ws.vcs_repo_url,
        )
        return
    owner, repo_name = parsed

    sha = sess.head_sha or previous.vcs_commit_sha
    try:
        run = await create(
            db,
            ws,
            conn,
            owner,
            repo_name,
            sha,
            previous.vcs_branch,
            speculative=ws.vcs_workflow != "apply_then_merge",
            pr_number=sess.pr_number,
            message=f"Re-plan for PR #{sess.pr_number} requested by {actor_login}",
            replaces_canceled=True,
        )
    except Exception as e:
        logger.warning(
            "plan: re-plan failed",
            workspace=ws.name,
            pr_number=sess.pr_number,
            error=repr(e),
        )
        return

    if run is None:
        # A live run already covers this SHA — the command is a no-op rather
        # than a failure (two `terrapod plan`s in quick succession).
        logger.info(
            "plan: a run already covers this head; nothing to re-plan",
            workspace=ws.name,
            pr_number=sess.pr_number,
        )
        return

    run.vcs_actor_login = actor_login
    run.vcs_actor_user_id = actor_user_id
    logger.info(
        "plan: created replacement run",
        workspace=ws.name,
        run_id=str(run.id),
        pr_number=sess.pr_number,
    )


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
