"""Slack interactive run approval (#556) — the blast-radius core.

A button click on an approval message carries no standing permission. Every click
re-derives the actor's authority *live*:

    verify (Socket Mode already authenticated the app) → resolve the durable
    (team,user)→email binding → resolve that email's LIVE Terrapod roles →
    build an AuthenticatedUser → resolve workspace capabilities → require
    `run:apply` → confirm/discard.

Unlinked clickers get an ephemeral nudge to `/terrapod link`; linked-but-
unauthorised clickers get an ephemeral "no permission" — neither mutates the run.
On success the shared approval message is edited in place to record who acted, so
the buttons can't be pressed twice and everyone sees the outcome. The binding is
identity, never entitlement: revoke the Terrapod role and the next click is denied
even though the Slack link still exists.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


async def handle_block_actions(payload: dict) -> None:
    """Process an approve/discard button click from an approval message."""
    from terrapod.services.slack_notify_service import ACTION_APPROVE, ACTION_DISCARD

    actions = payload.get("actions") or []
    if not actions:
        return
    action = actions[0]
    action_id = action.get("action_id")
    if action_id not in (ACTION_APPROVE, ACTION_DISCARD):
        return

    run_id = (action.get("value") or "").strip()
    team_id = (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get(
        "team_id", ""
    )
    user_id = (payload.get("user") or {}).get("id", "")
    response_url = payload.get("response_url", "")
    channel = (payload.get("channel") or {}).get("id", "")
    message_ts = (payload.get("message") or {}).get("ts", "")

    approve = action_id == ACTION_APPROVE
    try:
        await _act(approve, run_id, team_id, user_id, response_url, channel, message_ts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.interaction_failed", approve=approve, err=str(exc))


async def _nudge(response_url: str, text: str) -> None:
    # Ephemeral to the clicker only — must NOT replace the shared channel message.
    from terrapod.services.slack_link_service import post_response_url

    await post_response_url(response_url, text, replace_original=False)


async def _act(
    approve: bool,
    run_id: str,
    team_id: str,
    user_id: str,
    response_url: str,
    channel: str,
    message_ts: str,
) -> None:
    from terrapod.api.dependencies import AuthenticatedUser, _resolve_user_roles
    from terrapod.auth.capabilities import RUN_APPLY, has_capability
    from terrapod.db.models import Run, Workspace
    from terrapod.db.session import get_db_session
    from terrapod.services.run_service import confirm_run, discard_run
    from terrapod.services.slack_link_service import get_link
    from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

    verb = "Approve" if approve else "Discard"

    async with get_db_session() as db:
        # 1. Durable identity binding → email. No binding → nudge, no mutation.
        link = await get_link(db, team_id, user_id)
        if link is None:
            from terrapod.config import settings

            cmd = settings.slack.command
            await _nudge(
                response_url,
                f"You're not linked yet. Run `{cmd} link` to connect your Terrapod "
                "account, then try again.",
            )
            return
        email = link.terrapod_email

        run = await db.get(Run, run_id)
        if run is None:
            await _nudge(response_url, "That run no longer exists.")
            return
        workspace = await db.get(Workspace, run.workspace_id)
        if workspace is None:
            await _nudge(response_url, "That workspace no longer exists.")
            return

        # 2. LIVE roles for this email → AuthenticatedUser (identity ≠ entitlement).
        roles = await _resolve_user_roles(db, email)
        user = AuthenticatedUser(
            email=email,
            display_name=None,
            roles=roles,
            provider_name="slack",
            auth_method="session",
            kind="interactive",
        )

        ws_name = workspace.name  # captured for the deny message + parent edit

        # 3. Live capability check — the same gate the API/UI use.
        caps = await resolve_workspace_capabilities_for(db, user, workspace)
        if not has_capability(caps, RUN_APPLY):
            await _nudge(
                response_url,
                f"You ({email}) don't have permission to apply runs on *{ws_name}*.",
            )
            return

        # Capture the rest of the parent-edit primitives BEFORE commit
        # (expire_on_commit would detach these once the session closes).
        from terrapod.services.slack_notify_service import counts_text, run_url

        counts = counts_text(run)
        url = run_url(workspace.id, run.id)

        # 4. Confirm / discard. A stale button (run already resolved) raises
        #    ValueError — surface it ephemerally, don't 500.
        try:
            if approve:
                await confirm_run(db, run)
            else:
                await discard_run(db, run, reason=f"Discarded from Slack by {email}")
            await db.commit()
        except ValueError as exc:
            await _nudge(response_url, f"Couldn't {verb.lower()} this run: {exc}")
            return

    # 5. Edit the PARENT approval message: drop the buttons (no re-click) and
    #    record who acted. The apply/errored *result* arrives separately as a
    #    threaded reply from the notify handler, so this is the decision, not the
    #    outcome.
    status_line = (
        f":white_check_mark: Approved by {email} — applying…"
        if approve
        else f":wastebasket: Discarded by {email}"
    )
    await _resolve_parent(channel, message_ts, ws_name, counts, status_line, url)


async def _resolve_parent(
    channel: str, ts: str, ws_name: str, counts: str, status_line: str, url: str
) -> None:
    if not channel or not ts:
        return
    from terrapod.services.slack_notify_service import _bot_client, resolved_parent_blocks

    client = _bot_client()
    blocks = resolved_parent_blocks(ws_name, counts, status_line, url)
    try:
        await client.chat_update(channel=channel, ts=ts, blocks=blocks, text=status_line)
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.message_update_failed", err=str(exc))
