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
