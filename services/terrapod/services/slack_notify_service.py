"""Slack app run notifications (#556) — threaded per run.

**Opt-in per workspace** via ``slack_channel``; there is no deployment-wide
fan-out, so a channel gets traffic only because someone pointed a workspace at
it. Everything about one run lives in **one thread**, keeping the channel quiet:

* A run that needs manual approval posts a **parent** message with Approve /
  Discard buttons. The plan output (a ``.txt``), the approval decision
  (*Approved by X* — added by the interaction handler), and the final
  **apply / errored result** all thread underneath it. The result is a real
  threaded reply, so approvers get pinged when the run finishes.
* Auto-applied runs and drift alerts never wait for approval, so they post a
  single **standalone** message with the plan threaded under it.

Deep links use ``settings.external_url`` (the external *users'* host) only —
never an internal m2m host — and are omitted when unset. All I/O is async; the
plan file is streamed from storage to the ephemeral PVC, never buffered (rule 14).
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

# run_id → {"channel","ts"} of the parent approval message, so the plan file, the
# approval decision, and the terminal result all thread under it.
_MSGREF_PREFIX = "tp:slack:runmsg:"
_MSGREF_TTL = 7 * 24 * 3600

# Interaction action ids (consumed by the interaction handler).
ACTION_APPROVE = "terrapod_run_approve"
ACTION_DISCARD = "terrapod_run_discard"


# Events whose message carries a *freshly generated* AI review (the plan
# summary / failure analysis). When AI is enabled, we defer these until the
# summary settles so the message carries the review from its first post — the
# summariser re-fires them with `_from_summariser=True`. `completed` is NOT here
# (its summary is the plan summary, already ready by apply time), nor is
# `drift_detected` (it has a suppress-no-changes gate that lives in the drift
# handler, so it must fire from there).
_AI_DEFERRABLE = frozenset({"run:needs_attention", "run:errored"})


async def enqueue_slack_notify(run, trigger: str, *, _from_summariser: bool = False) -> None:
    """Enqueue a Slack run notification. Called alongside the existing
    notification-deliver enqueue; the handler no-ops if the workspace hasn't
    opted in, so this is safe to fire unconditionally for the four triggers.

    When AI plan summaries are enabled, the fresh-AI events are deferred to the
    summariser's completion (see `_AI_DEFERRABLE`) so the message includes the
    review from the first post rather than racing it."""
    if trigger not in _SLACK_TRIGGERS:
        return
    from terrapod.config import settings

    # The `slack_run_notify` trigger handler is only registered when the Slack
    # app is enabled (app.py). Enqueuing when it's disabled would land an item
    # with no handler — a "No handler for trigger type" warning on *every* run
    # for every non-Slack deployment. Gate here (this fires from the run state
    # machine + drift on every transition, Slack-configured or not).
    if not settings.slack.enabled:
        return

    if not _from_summariser and settings.ai_summary.enabled and trigger in _AI_DEFERRABLE:
        return  # the summariser will re-fire this once the AI review settles
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


def run_url(workspace_id, run_id) -> str:
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


def counts_text(run) -> str:
    if run.resource_additions is None:
        return ""
    return (
        f"`+{run.resource_additions or 0}` "
        f"`~{run.resource_changes or 0}` "
        f"`-{run.resource_destructions or 0}`"
    )


def _link_ctx(url: str) -> list:
    """Footer context: the deep link plus this deployment's label (#691), so a
    shared Slack channel can tell multiple Terrapod deployments apart. Either
    element is omitted when unset; the whole block drops if both are empty."""
    from terrapod.config import settings

    elements = []
    if url:
        elements.append({"type": "mrkdwn", "text": f"<{url}|Open in Terrapod>"})
    label = (settings.slack.label or "").strip()
    if label:
        elements.append({"type": "mrkdwn", "text": f"Terrapod: *{label}*"})
    return [{"type": "context", "elements": elements}] if elements else []


def resolved_parent_blocks(ws_name: str, counts: str, status_line: str, url: str) -> list:
    """Blocks for the approval message AFTER it's resolved — no buttons, with a
    status line (e.g. ``:white_check_mark: Approved by X``). Used by the
    interaction handler to disable re-clicking and record who acted. ``counts``
    is a pre-rendered string (captured before the DB session closes)."""
    blocks: list = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{ws_name}* — run approval"}}
    ]
    if counts:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"Changes: {counts}"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": status_line}]})
    blocks += _link_ctx(url)
    return blocks


async def _upload_plan_file(
    client, channel: str, ws_id: str, run_id: str, *, thread_ts: str | None = None
) -> None:
    """Stream the plan log from storage → PVC temp → Slack file (threaded under
    the run's message). Best-effort."""
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
        kwargs = {"channel": channel, "file": tmp, "filename": "plan.txt", "title": "Plan output"}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await client.files_upload_v2(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.plan_file_upload_failed", err=str(exc))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def _build_message(db: AsyncSession, run, workspace, trigger: str) -> dict:
    """Return {'blocks': [...], 'text': fallback} for the given event."""
    url = run_url(workspace.id, run.id)
    ws = workspace.name
    counts = counts_text(run)
    ai = await _ai_blocks(db, run)

    if trigger == "run:needs_attention":
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":hourglass_flowing_sand: *{ws}* — a run needs your approval",
                },
            }
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
        return {"blocks": blocks, "text": f"{ws}: a run needs approval"}

    if trigger == "run:completed":
        head = f":white_check_mark: *{ws}* — applied"
        if counts:
            head += f"  {counts}"
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
        blocks += ai + _link_ctx(url)
        return {"blocks": blocks, "text": f"{ws}: applied"}

    if trigger == "run:errored":
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":x: *{ws}* — run errored"}}
        ]
        blocks += ai + _link_ctx(url)
        return {"blocks": blocks, "text": f"{ws}: run errored"}

    # run:drift_detected
    head = f":warning: *{ws}* — drift detected"
    if counts:
        head += f"  {counts}"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    blocks += ai + _link_ctx(url)
    return {"blocks": blocks, "text": f"{ws}: drift detected"}


async def handle_slack_run_notify(payload: dict) -> None:
    """Triggered task: post/thread the Slack message for a run event (opt-in)."""
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
        ws_id = str(run.workspace_id)
        message = await _build_message(db, run, workspace, trigger)

    client = _bot_client()
    redis = get_redis_client()
    ref_key = f"{_MSGREF_PREFIX}{run_id}"

    try:
        if trigger == "run:needs_attention":
            # Idempotent: the approval parent may be posted by the summariser
            # (deferred, AI-bearing) OR by the backfill safety net. Whichever
            # runs first wins; a second fire must not post a duplicate parent.
            if await redis.hgetall(ref_key):
                return
            resp = await client.chat_postMessage(
                channel=channel, blocks=message["blocks"], text=message["text"]
            )
            if resp.get("ok"):
                await redis.hset(ref_key, mapping={"channel": resp["channel"], "ts": resp["ts"]})
                await redis.expire(ref_key, _MSGREF_TTL)
                await _upload_plan_file(
                    client, resp["channel"], ws_id, str(run_id), thread_ts=resp["ts"]
                )
            return

        # Terminal / drift events. If there's an approval parent, thread the
        # result under it (the follow-up ping); else post standalone.
        existing = await redis.hgetall(ref_key)
        parent_ch = existing.get("channel") if existing else None
        parent_ts = existing.get("ts") if existing else None
        if parent_ch and parent_ts:
            await client.chat_postMessage(
                channel=parent_ch,
                thread_ts=parent_ts,
                blocks=message["blocks"],
                text=message["text"],
            )
            return

        resp = await client.chat_postMessage(
            channel=channel, blocks=message["blocks"], text=message["text"]
        )
        if resp.get("ok") and trigger in ("run:completed", "run:drift_detected"):
            await _upload_plan_file(
                client, resp["channel"], ws_id, str(run_id), thread_ts=resp["ts"]
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("slack.run_notify_failed", trigger=trigger, err=str(exc))


# When AI plan summaries are enabled, the needs-approval post is deferred to the
# summariser (so the AI review is in the first render). But the summariser is
# enqueued off the plan-JSON upload — if the runner dies between the plan-result
# POST and that upload, the summariser never runs and the approval prompt is
# never posted, leaving the approver waiting silently (#687). This backfill is
# the safety net: a run that has sat in `planned` awaiting manual apply for
# longer than the grace window (so we're past the happy-path race with the
# summariser) with no parent message yet gets posted now — without the AI review
# if it genuinely never arrived. Delivery is guaranteed; the AI review is not.
_BACKFILL_GRACE_SECONDS = 180


async def slack_approval_backfill_cycle() -> None:
    """Periodic safety net: post any deferred needs-approval message the
    summariser never fired (see the note above). Registered only when the Slack
    app is on; short-circuits when AI is off (nothing is deferred then)."""
    from datetime import timedelta
    from types import SimpleNamespace

    from terrapod.config import settings
    from terrapod.db.models import Run, Workspace, now_utc
    from terrapod.db.session import get_db_session
    from terrapod.redis.client import get_redis_client

    if not settings.slack.enabled or not settings.ai_summary.enabled:
        return  # deferral only happens with AI on → nothing to back-fill otherwise

    cutoff = now_utc() - timedelta(seconds=_BACKFILL_GRACE_SECONDS)
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(Run.id, Run.workspace_id)
                .join(Workspace, Workspace.id == Run.workspace_id)
                .where(
                    Run.status == "planned",
                    Run.auto_apply.is_(False),
                    Run.plan_only.is_(False),
                    Run.is_drift_detection.is_(False),
                    Run.plan_finished_at.is_not(None),
                    Run.plan_finished_at < cutoff,
                    Workspace.slack_channel != "",
                )
                .limit(200)
            )
        ).all()

    if not rows:
        return
    redis = get_redis_client()
    for run_id, ws_id in rows:
        if await redis.hgetall(f"{_MSGREF_PREFIX}{run_id}"):
            continue  # already posted (by the summariser or a prior backfill)
        logger.info("slack.approval_backfill", run_id=str(run_id))
        # _from_summariser=True bypasses the AI-defer so it posts now; the
        # handler is idempotent, so a late summariser fire won't double-post.
        await enqueue_slack_notify(
            SimpleNamespace(id=run_id, workspace_id=ws_id),
            "run:needs_attention",
            _from_summariser=True,
        )
