"""Tests for the /terrapod Slack slash-command handler (#556)."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import slack_commands as sc


@pytest.mark.asyncio
async def test_link_returns_connect_button_with_state_url():
    with (
        patch(
            "terrapod.services.slack_link_service.mint_link_state",
            new=AsyncMock(return_value="ST8"),
        ),
        patch.object(settings, "external_url", "https://terrapod.example.com"),
    ):
        resp = await sc.build_slash_response("link", "T1", "U1")
    assert resp["response_type"] == "ephemeral"
    button = resp["blocks"][1]["elements"][0]
    assert button["url"] == "https://terrapod.example.com/slack/link?state=ST8"


@pytest.mark.asyncio
async def test_unknown_subcommand_returns_usage():
    resp = await sc.build_slash_response("wat", "T1", "U1")
    assert "Usage:" in resp["text"]
    # Usage echoes the default command.
    assert "/terrapod link" in resp["text"]


@pytest.mark.asyncio
async def test_usage_echoes_configured_command():
    """Multi-deployment (#691): help text uses this deployment's command, not a
    hardcoded /terrapod, so a second deployment's /terrapod-prod reads right."""
    with patch.object(settings.slack, "command", "/terrapod-prod"):
        resp = await sc.build_slash_response("wat", "T1", "U1")
    assert "/terrapod-prod link" in resp["text"]
    assert "/terrapod link" not in resp["text"]


@pytest.mark.asyncio
async def test_socket_request_matches_configured_command():
    """The socket listener answers this deployment's configured command and acks
    (no reply) for the default /terrapod when it's been reconfigured away."""
    client = MagicMock()
    client.send_socket_mode_response = AsyncMock()
    with patch.object(settings.slack, "command", "/terrapod-prod"):
        # The configured command → ephemeral reply payload.
        req = SimpleNamespace(
            type="slash_commands",
            envelope_id="e1",
            payload={"command": "/terrapod-prod", "text": "help", "team_id": "T", "user_id": "U"},
        )
        await sc.handle_socket_request(client, req)
        assert client.send_socket_mode_response.await_args.args[0].payload is not None
        # The bare /terrapod is now a foreign command for this deployment → no reply.
        req2 = SimpleNamespace(
            type="slash_commands",
            envelope_id="e2",
            payload={"command": "/terrapod", "text": "help", "team_id": "T", "user_id": "U"},
        )
        await sc.handle_socket_request(client, req2)
        assert client.send_socket_mode_response.await_args.args[0].payload is None


@pytest.mark.asyncio
async def test_status_reports_binding():
    @asynccontextmanager
    async def _fake_session():
        yield MagicMock()

    link = SimpleNamespace(terrapod_email="alice@example.com")
    with (
        patch("terrapod.db.session.get_db_session", _fake_session),
        patch("terrapod.services.slack_link_service.get_link", new=AsyncMock(return_value=link)),
    ):
        resp = await sc.build_slash_response("status", "T1", "U1")
    assert "alice@example.com" in resp["text"]


@pytest.mark.asyncio
async def test_socket_request_acks_terrapod_command_with_reply():
    sent = []
    client = MagicMock()
    client.send_socket_mode_response = AsyncMock(side_effect=lambda r: sent.append(r))
    req = SimpleNamespace(
        type="slash_commands",
        envelope_id="e1",
        payload={"command": "/terrapod", "text": "help", "team_id": "T", "user_id": "U"},
    )
    await sc.handle_socket_request(client, req)
    assert client.send_socket_mode_response.await_count == 1
    # The ack carries the ephemeral reply payload for a slash command.
    assert sent[0].payload is not None


@pytest.mark.asyncio
async def test_socket_request_acks_foreign_command_without_reply():
    client = MagicMock()
    client.send_socket_mode_response = AsyncMock()
    req = SimpleNamespace(
        type="slash_commands",
        envelope_id="e2",
        payload={"command": "/somethingelse", "text": "", "team_id": "T", "user_id": "U"},
    )
    await sc.handle_socket_request(client, req)
    # Ack'd, but with no reply payload (not our command).
    sent = client.send_socket_mode_response.await_args.args[0]
    assert sent.payload is None


@pytest.mark.asyncio
async def test_interactive_block_actions_acks_then_dispatches():
    """A button click acks within Slack's 3s window, then dispatches to the
    interaction handler (the RBAC + confirm/discard spine)."""
    from unittest.mock import patch

    client = MagicMock()
    client.send_socket_mode_response = AsyncMock()
    payload = {"type": "block_actions", "actions": [{"action_id": "terrapod_run_approve"}]}
    req = SimpleNamespace(type="interactive", envelope_id="e3", payload=payload)
    with patch(
        "terrapod.services.slack_interactions.handle_block_actions", new_callable=AsyncMock
    ) as hba:
        await sc.handle_socket_request(client, req)
    # ack'd (empty payload) AND dispatched to the interaction handler
    sent = client.send_socket_mode_response.await_args.args[0]
    assert sent.payload is None
    hba.assert_awaited_once_with(payload)
