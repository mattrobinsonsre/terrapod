"""Slack integration — Socket Mode connection lifecycle (#556).

Phase 1 establishes the outbound **Socket Mode** WebSocket to Slack and posts a
connectivity check to the configured channel, so operators can confirm the app
is wired end to end before any approval logic exists. Later phases attach
interaction handlers (approve/discard) and the `/terrapod` slash command to the
same connection.

Design notes:
- **Outbound only.** Socket Mode dials *out* to Slack; the API needs no public
  URL or inbound firewall rule (same posture as the runner listeners).
- **Multi-replica safe.** Every API replica may hold its own Socket Mode
  connection; Slack load-balances each event to exactly one connection, so no
  leader election is needed. The one-time connectivity post is de-duplicated
  across replicas via a Redis `SET NX` guard so the channel is greeted once.
- **All I/O is async** (`slack_sdk` aiohttp clients) per the no-sync-in-async
  rule; nothing here blocks the event loop.

See docs/slack-integration.md for operator setup.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# Module-global handle so the lifespan can disconnect cleanly on shutdown.
_socket_client = None

_HELLO_DEDUP_KEY = "tp:slack:hello_posted"
_HELLO_DEDUP_TTL = 300  # seconds — one greeting per deployment start window


async def start_slack(settings) -> None:
    """Open the Socket Mode connection if Slack is enabled and configured.

    Best-effort: a misconfiguration logs and returns rather than failing API
    startup — Slack is an accessory, never a hard dependency of the API.
    """
    cfg = settings.slack
    if not cfg.enabled:
        return
    if not cfg.socket_mode:
        # Request-URL mode is wired at the ingress/receiver layer, not here.
        logger.info("slack.request_url_mode_selected_socket_start_skipped")
        return
    if not (cfg.app_token and cfg.bot_token):
        logger.warning(
            "slack.enabled_but_tokens_missing",
            have_app_token=bool(cfg.app_token),
            have_bot_token=bool(cfg.bot_token),
        )
        return

    try:
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError:
        logger.error("slack.slack_sdk_not_installed")
        return

    global _socket_client
    web = AsyncWebClient(token=cfg.bot_token)
    _socket_client = SocketModeClient(app_token=cfg.app_token, web_client=web)

    # Dispatch /terrapod slash commands (+ ack interactions) over the socket (#556).
    from terrapod.services.slack_commands import handle_socket_request

    _socket_client.socket_mode_request_listeners.append(handle_socket_request)

    try:
        await _socket_client.connect()
    except Exception as exc:  # noqa: BLE001
        logger.error("slack.socket_mode_connect_failed", err=str(exc))
        _socket_client = None
        return

    logger.info("slack.socket_mode_connected")

    if cfg.default_channel:
        await _post_hello(web, cfg.default_channel)


async def _post_hello(web, channel: str) -> None:
    """Post a one-time connectivity check, de-duplicated across replicas."""
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        # Only the replica that wins the SET NX greets the channel.
        won = await redis.set(_HELLO_DEDUP_KEY, "1", nx=True, ex=_HELLO_DEDUP_TTL)
        if not won:
            return
    except Exception as exc:  # noqa: BLE001
        # If Redis is unavailable, greet anyway (dev/single-replica) — the
        # connectivity check matters more than perfect de-duplication.
        logger.debug("slack.hello_dedup_unavailable", err=str(exc))

    try:
        await web.chat_postMessage(
            channel=channel,
            text=":white_check_mark: Terrapod is connected to Slack (Socket Mode).",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.hello_post_failed", channel=channel, err=str(exc))


async def stop_slack() -> None:
    """Disconnect the Socket Mode connection on API shutdown."""
    global _socket_client
    if _socket_client is not None:
        try:
            await _socket_client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        _socket_client = None
