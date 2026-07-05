"""Slack slash-command handling over Socket Mode (#556).

`/terrapod link | status | unlink` — account linking from the channel. `link`
mints a signed, single-use state and returns an ephemeral "Connect your Terrapod
account" link that drives the user through Terrapod's own login; `status` /
`unlink` operate on the caller's existing binding.

Interactive payloads (approve/discard buttons) are ack'd here and dispatched to
``slack_interactions.handle_block_actions`` for the live RBAC + confirm/discard.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def _connect_url(state: str) -> str:
    from terrapod.config import settings

    base = (settings.external_url or "").rstrip("/")
    return f"{base}/slack/link?state={state}"


async def _handle_link(team_id: str, user_id: str, response_url: str = "") -> dict:
    from terrapod.services.slack_link_service import mint_link_state

    url = _connect_url(await mint_link_state(team_id, user_id, response_url))
    return {
        "response_type": "ephemeral",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Connect your Terrapod account* to act on runs from Slack.",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Connect Terrapod"},
                        "url": url,
                        "style": "primary",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "This link is single-use and expires in 10 minutes."}
                ],
            },
        ],
    }


async def _handle_status(team_id: str, user_id: str) -> dict:
    from terrapod.db.session import get_db_session
    from terrapod.services.slack_link_service import get_link

    async with get_db_session() as db:
        link = await get_link(db, team_id, user_id)
    if link:
        return {
            "response_type": "ephemeral",
            "text": f":white_check_mark: Linked to Terrapod as *{link.terrapod_email}*.",
        }
    from terrapod.config import settings

    cmd = settings.slack.command
    return {
        "response_type": "ephemeral",
        "text": f"Not linked yet. Run `{cmd} link` to connect your Terrapod account.",
    }


async def _handle_unlink(team_id: str, user_id: str) -> dict:
    from terrapod.db.session import get_db_session
    from terrapod.services.slack_link_service import unlink

    async with get_db_session() as db:
        removed = await unlink(db, team_id, user_id)
    if removed:
        return {
            "response_type": "ephemeral",
            "text": "Your Terrapod account has been unlinked from Slack.",
        }
    return {"response_type": "ephemeral", "text": "You had no Terrapod link to remove."}


async def build_slash_response(
    text: str, team_id: str, user_id: str, response_url: str = ""
) -> dict:
    """Dispatch a `/terrapod <sub>` command to an ephemeral response payload."""
    parts = (text or "").strip().split()
    sub = parts[0].lower() if parts else "help"
    if sub == "link":
        return await _handle_link(team_id, user_id, response_url)
    if sub == "status":
        return await _handle_status(team_id, user_id)
    if sub == "unlink":
        return await _handle_unlink(team_id, user_id)
    from terrapod.config import settings

    cmd = settings.slack.command
    return {
        "response_type": "ephemeral",
        "text": f"Usage: `{cmd} link` · `{cmd} status` · `{cmd} unlink`",
    }


async def handle_socket_request(client, req) -> None:
    """slack_sdk Socket Mode request listener: ack + respond to /terrapod commands."""
    try:
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError:
        return

    async def _ack(payload=None):
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id, payload=payload)
        )

    if req.type == "interactive":
        # Ack within Slack's 3s window FIRST, then do the (slower) RBAC + apply
        # work — Slack only needs the ack to dismiss the button spinner.
        await _ack()
        payload = req.payload or {}
        if payload.get("type") == "block_actions":
            from terrapod.services.slack_interactions import handle_block_actions

            await handle_block_actions(payload)
        return

    if req.type != "slash_commands":
        await _ack()  # ack other events; nothing to act on
        return

    from terrapod.config import settings

    payload = req.payload or {}
    if payload.get("command") != settings.slack.command:
        await _ack()
        return

    try:
        response = await build_slash_response(
            payload.get("text", ""),
            payload.get("team_id", ""),
            payload.get("user_id", ""),
            payload.get("response_url", ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.slash_command_failed", err=str(exc))
        response = {"response_type": "ephemeral", "text": "Something went wrong handling that."}

    # For slash commands the ack payload IS the ephemeral reply.
    await _ack(response)
