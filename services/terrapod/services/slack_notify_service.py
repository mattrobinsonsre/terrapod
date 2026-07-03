"""Slack app run notifications (#556) — approval / applied / errored / drift.

**Opt-in per workspace:** a message posts only when the Slack app is enabled AND
the workspace has its own ``slack_channel`` set. There is no default fan-out — the
config-level ``slack.default_channel`` is only the connectivity check — so a
channel gets run traffic solely because someone deliberately pointed a workspace
at it. Quiet by default, exactly as loud as you opt into.

Events (each fires once, deduped):
  * **needs_attention** → interactive message with Approve / Discard buttons.
  * **applied** / **errored** → outcome; if an approval message exists for the run
    it is *updated in place* (closing the loop) rather than posting anew.
  * **drift_detected** → informational drift alert.

Deep links use ``settings.external_url`` (the external *users'* host) only — never
an internal m2m host — and are omitted when it is unset. All I/O is async; the
plan file is streamed from storage to the ephemeral PVC, never buffered whole
(rule 14).
"""

from __future__ import annotations

import os
import tempfile

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# Triggers we mirror to Slack. `run:planned` (auto-apply / plan-only / speculative)
# is deliberately absent — only actionable or terminal events, never noise.
_SLACK_TRIGGERS = frozenset(
    {"run:needs_attention", "run:completed", "run:errored", "run:drift_detected"}
)

# run_id → {"channel","ts"} of the posted message, so applied/errored can update
# the approval message in place (outcome closure).
_MSGREF_PREFIX = "tp:slack:runmsg:"
_MSGREF_TTL = 7 * 24 * 3600

# Interaction action ids (consumed by the interaction handler).
ACTION_APPROVE = "terrapod_run_approve"
ACTION_DISCARD = "terrapod_run_discard"


async def enqueue_slack_notify(run, trigger: str) -> None:
    """Enqueue a Slack run notification. Called alongside the existing
    notification-deliver enqueue; the handler no-ops if the workspace hasn't
    opted in, so this is safe to fire unconditionally for the four triggers."""
    if trigger not in _SLACK_TRIGGERS:
        return
    from terrapod.services.scheduler import enqueue_trigger

    try:
        await enqueue_trigger(
            "slack_run_notify",
            {"run_id": str(run.id), "workspace_id": str(run.workspace_id), "trigger": trigger},
            dedup_key=f"slacknotif:{run.id}:{trigger}",
            dedup_ttl=60,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("slack.notify_enqueue_failed", err=str(e))


def _bot_client():
    from slack_sdk.web.async_client import AsyncWebClient

    from terrapod.config import settings

    return AsyncWebClient(token=settings.slack.bot_token)


def _run_url(workspace_id, run_id) -> str:
    from terrapod.config import settings

    base = (settings.external_url or "").rstrip("/")
    return f"{base}/workspaces/{workspace_id}/runs/{run_id}" if base else ""


def _resolve_tmpdir() -> str | None:
    from terrapod.config import settings

    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


async def _ai_blocks(db: AsyncSession, run) -> list:
    """Compact AI summary block(s) if a ready summary exists — never dominates."""
    from terrapod.db.models import PlanSummary

    ps = (
        await db.execute(select(PlanSummary).where(PlanSummary.run_id == run.id))
    ).scalar_one_or_none()
    if not ps or ps.status != "ready" or not (ps.description or "").strip():
        return []
    desc = ps.description.strip()
    if len(desc) > 700:
        desc = desc[:697] + "…"
    risk = (ps.risk_level or "").upper() or "n/a"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*AI review* — risk *{risk}*\n{desc}"},
        }
    ]


def _counts_text(run) -> str:
    if run.resource_additions is None:
        return ""
    return (
        f"`+{run.resource_additions or 0}` "
        f"`~{run.resource_changes or 0}` "
        f"`-{run.resource_destructions or 0}`"
    )


async def _upload_plan_file(client, channel: str, ws_id: str, run_id: str) -> None:
    """Stream the plan log from storage → PVC temp → Slack file. Best-effort."""
    from terrapod.storage import get_storage
    from terrapod.storage.keys import plan_log_key

    key = plan_log_key(ws_id, run_id)
    storage = get_storage()
    try:
        if not await storage.exists(key):
            return
    except Exception:  # noqa: BLE001
        return

    fd, tmp = tempfile.mkstemp(suffix=".plan.txt", dir=_resolve_tmpdir())
    os.close(fd)
    try:
        import aiofiles

        async with aiofiles.open(tmp, "wb") as fh:
            async for chunk in storage.get_stream(key):
                await fh.write(chunk)
        await client.files_upload_v2(
            channel=channel, file=tmp, filename="plan.txt", title="Terraform plan output"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.plan_file_upload_failed", err=str(exc))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _link_ctx(url: str) -> list:
    return (
        [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{url}|Open in Terrapod>"}]}]
        if url
        else []
    )


async def _build_message(db: AsyncSession, run, workspace, trigger: str) -> dict:
    """Return {'blocks': [...], 'text': fallback, 'interactive': bool}."""
    url = _run_url(workspace.id, run.id)
    ws = workspace.name
    counts = _counts_text(run)
    ai = await _ai_blocks(db, run)

    if trigger == "run:needs_attention":
        header = f":hourglass_flowing_sand: *{ws}* — a run needs your approval"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]
        if counts:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"Changes: {counts}"}}
            )
        blocks += ai
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": ACTION_APPROVE,
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "value": str(run.id),
                    },
                    {
                        "type": "button",
                        "action_id": ACTION_DISCARD,
                        "text": {"type": "plain_text", "text": "Discard"},
                        "style": "danger",
                        "value": str(run.id),
                    },
                ],
            }
        )
        blocks += _link_ctx(url)
        return {"blocks": blocks, "text": f"{ws}: a run needs approval", "interactive": True}

    if trigger == "run:completed":
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":white_check_mark: *{ws}* — applied"},
            }
        ]
        if counts:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"Changes: {counts}"}}
            )
        blocks += ai + _link_ctx(url)
        return {"blocks": blocks, "text": f"{ws}: applied", "interactive": False}

    if trigger == "run:errored":
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":x: *{ws}* — run errored"}}
        ]
        blocks += ai + _link_ctx(url)
        return {"blocks": blocks, "text": f"{ws}: run errored", "interactive": False}

    # run:drift_detected
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":warning: *{ws}* — drift detected"},
        }
    ]
    if counts:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"Drift: {counts}"}})
    blocks += ai + _link_ctx(url)
    return {"blocks": blocks, "text": f"{ws}: drift detected", "interactive": False}


async def handle_slack_run_notify(payload: dict) -> None:
    """Triggered task: post/update the Slack message for a run event (opt-in)."""
    from terrapod.config import settings
    from terrapod.db.models import Run, Workspace
    from terrapod.db.session import get_db_session
    from terrapod.redis.client import get_redis_client

    if not settings.slack.enabled or not settings.slack.bot_token:
        return

    run_id = payload.get("run_id")
    trigger = payload.get("trigger", "")
    if not run_id or trigger not in _SLACK_TRIGGERS:
        return

    async with get_db_session() as db:
        run = await db.get(Run, run_id)
        if run is None:
            return
        workspace = await db.get(Workspace, run.workspace_id)
        if workspace is None:
            return
        channel = (workspace.slack_channel or "").strip()
        if not channel:  # opt-in: no channel → no message
            return
        ws_id = str(run.workspace_id)  # capture before the session closes
        message = await _build_message(db, run, workspace, trigger)

    client = _bot_client()
    redis = get_redis_client()
    ref_key = f"{_MSGREF_PREFIX}{run_id}"

    try:
        if trigger in ("run:completed", "run:errored"):
            # Outcome closure: update the approval message in place if we have one
            # (the Redis client decodes responses, so these are plain str).
            existing = await redis.hgetall(ref_key)
            if existing and existing.get("channel") and existing.get("ts"):
                await client.chat_update(
                    channel=existing["channel"],
                    ts=existing["ts"],
                    blocks=message["blocks"],
                    text=message["text"],
                )
                return
        resp = await client.chat_postMessage(
            channel=channel, blocks=message["blocks"], text=message["text"]
        )
        if message["interactive"] and resp.get("ok"):
            await redis.hset(ref_key, mapping={"channel": resp["channel"], "ts": resp["ts"]})
            await redis.expire(ref_key, _MSGREF_TTL)
        # Attach the plan output as a file (best-effort) for plan-bearing events.
        if trigger in ("run:needs_attention", "run:completed", "run:drift_detected"):
            await _upload_plan_file(client, channel, ws_id, str(run_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.run_notify_failed", trigger=trigger, err=str(exc))
