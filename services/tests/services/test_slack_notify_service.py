"""Tests for Slack run notifications (#556): opt-in gate, trigger filter, shapes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import slack_notify_service as sn


def _run(**kw):
    base = {
        "id": "run-1",
        "workspace_id": "ws-1",
        "resource_additions": 1,
        "resource_changes": 2,
        "resource_destructions": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_db_no_ai():
    """db.execute(...).scalar_one_or_none() → None (no AI summary)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    return db


@pytest.mark.asyncio
async def test_enqueue_filters_non_slack_triggers():
    enq = AsyncMock()
    with patch("terrapod.services.scheduler.enqueue_trigger", enq):
        await sn.enqueue_slack_notify(_run(), "run:planning")
        await sn.enqueue_slack_notify(_run(), "run:planned")
        await sn.enqueue_slack_notify(_run(), "run:created")
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_passes_the_four_slack_triggers():
    enq = AsyncMock()
    with patch("terrapod.services.scheduler.enqueue_trigger", enq):
        for t in ("run:needs_attention", "run:completed", "run:errored", "run:drift_detected"):
            await sn.enqueue_slack_notify(_run(), t)
    assert enq.await_count == 4
    # dedup key is per (run, trigger)
    keys = {c.kwargs["dedup_key"] for c in enq.await_args_list}
    assert keys == {
        f"slacknotif:run-1:{t}"
        for t in ("run:needs_attention", "run:completed", "run:errored", "run:drift_detected")
    }


@pytest.mark.asyncio
async def test_needs_attention_message_has_approve_discard_buttons():
    db = _fake_db_no_ai()
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod")
    with patch.object(settings, "external_url", "https://terrapod.example.com"):
        msg = await sn._build_message(db, run, ws, "run:needs_attention")
    assert msg["interactive"] is True
    action_blocks = [b for b in msg["blocks"] if b.get("type") == "actions"]
    assert action_blocks, "needs_attention must carry an actions block"
    action_ids = {e["action_id"] for e in action_blocks[0]["elements"]}
    assert action_ids == {sn.ACTION_APPROVE, sn.ACTION_DISCARD}
    # the run values are the run id (what the interaction handler reads)
    assert all(e["value"] == "run-1" for e in action_blocks[0]["elements"])


@pytest.mark.asyncio
async def test_terminal_messages_have_no_buttons():
    db = _fake_db_no_ai()
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod")
    for trigger in ("run:completed", "run:errored", "run:drift_detected"):
        with patch.object(settings, "external_url", "https://terrapod.example.com"):
            msg = await sn._build_message(db, run, ws, trigger)
        assert msg["interactive"] is False
        assert not [b for b in msg["blocks"] if b.get("type") == "actions"]


@pytest.mark.asyncio
async def test_deep_link_omitted_when_external_url_unset():
    db = _fake_db_no_ai()
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod")
    with patch.object(settings, "external_url", ""):
        msg = await sn._build_message(db, run, ws, "run:completed")
    # no "Open in Terrapod" context element when external_url is unset
    ctxs = [b for b in msg["blocks"] if b.get("type") == "context"]
    joined = str(ctxs)
    assert "Open in Terrapod" not in joined


@pytest.mark.asyncio
async def test_deep_link_uses_external_url_only():
    ws = SimpleNamespace(id="ws-1", name="prod")
    run = _run()
    db = _fake_db_no_ai()
    with patch.object(settings, "external_url", "https://users.example.com"):
        msg = await sn._build_message(db, run, ws, "run:completed")
    assert "https://users.example.com/workspaces/ws-1/runs/run-1" in str(msg["blocks"])


@pytest.mark.asyncio
async def test_handler_noop_when_workspace_not_opted_in():
    """Opt-in: a workspace with no slack_channel posts nothing."""
    run = _run()
    ws = SimpleNamespace(id="ws-1", name="prod", slack_channel="")

    class CM:
        async def __aenter__(self):
            return SimpleNamespace(get=AsyncMock(side_effect=[run, ws]))

        async def __aexit__(self, *a):
            return False

    bot = MagicMock()
    with (
        patch.object(settings.slack, "enabled", True),
        patch.object(settings.slack, "bot_token", "xoxb-x"),
        patch("terrapod.db.session.get_db_session", return_value=CM()),
        patch("terrapod.services.slack_notify_service._bot_client", return_value=bot),
    ):
        await sn.handle_slack_run_notify(
            {"run_id": "run-1", "workspace_id": "ws-1", "trigger": "run:needs_attention"}
        )
    bot.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
async def test_handler_noop_when_slack_disabled():
    bot = MagicMock()
    with (
        patch.object(settings.slack, "enabled", False),
        patch("terrapod.services.slack_notify_service._bot_client", return_value=bot),
    ):
        await sn.handle_slack_run_notify(
            {"run_id": "run-1", "workspace_id": "ws-1", "trigger": "run:needs_attention"}
        )
    bot.chat_postMessage.assert_not_called()
