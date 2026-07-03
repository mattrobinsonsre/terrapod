"""Tests for the Slack Socket Mode connection lifecycle (#556)."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import slack_service


def _settings(**slack):
    base = {
        "enabled": False,
        "socket_mode": True,
        "bot_token": "",
        "app_token": "",
        "signing_secret": "",
    }
    base.update(slack)
    return SimpleNamespace(slack=SimpleNamespace(**base))


@pytest.mark.asyncio
async def test_disabled_does_not_connect():
    slack_service._socket_client = None
    await slack_service.start_slack(_settings(enabled=False))
    assert slack_service._socket_client is None


@pytest.mark.asyncio
async def test_enabled_but_missing_tokens_does_not_connect():
    slack_service._socket_client = None
    await slack_service.start_slack(_settings(enabled=True, app_token="", bot_token=""))
    assert slack_service._socket_client is None


@pytest.mark.asyncio
async def test_socket_mode_false_skips_socket_start():
    slack_service._socket_client = None
    await slack_service.start_slack(
        _settings(enabled=True, socket_mode=False, app_token="xapp-x", bot_token="xoxb-x")
    )
    assert slack_service._socket_client is None


@pytest.mark.asyncio
async def test_enabled_with_tokens_connects_and_auth_tests():
    slack_service._socket_client = None

    client_inst = MagicMock()
    client_inst.connect = AsyncMock()
    client_inst.disconnect = AsyncMock()
    web_inst = MagicMock()
    # Connectivity check is `auth.test` (logged) — NOT a channel greeting.
    web_inst.auth_test = AsyncMock(return_value={"team": "T", "user": "terrapod"})
    web_inst.chat_postMessage = AsyncMock()

    sock_mod = MagicMock()
    sock_mod.SocketModeClient = MagicMock(return_value=client_inst)
    web_mod = MagicMock()
    web_mod.AsyncWebClient = MagicMock(return_value=web_inst)
    fake_modules = {
        "slack_sdk": MagicMock(),
        "slack_sdk.socket_mode": MagicMock(),
        "slack_sdk.socket_mode.aiohttp": sock_mod,
        "slack_sdk.web": MagicMock(),
        "slack_sdk.web.async_client": web_mod,
    }

    with patch.dict(sys.modules, fake_modules):
        await slack_service.start_slack(
            _settings(
                enabled=True,
                socket_mode=True,
                app_token="xapp-x",
                bot_token="xoxb-x",
            )
        )

    client_inst.connect.assert_awaited_once()
    web_inst.auth_test.assert_awaited_once()
    # No startup banner posted to any channel (that was noise).
    web_inst.chat_postMessage.assert_not_called()
    assert slack_service._socket_client is client_inst

    await slack_service.stop_slack()
    client_inst.disconnect.assert_awaited_once()
    assert slack_service._socket_client is None


def test_slack_config_defaults_and_nested_env(monkeypatch):
    # Secret tokens map from the nested env vars the chart injects via secretKeyRef.
    from terrapod.config import Settings

    s = Settings()
    assert s.slack.enabled is False
    assert s.slack.socket_mode is True
    assert s.slack.bot_token == ""

    monkeypatch.setenv("TERRAPOD_SLACK__BOT_TOKEN", "xoxb-from-env")
    s2 = Settings()
    assert s2.slack.bot_token == "xoxb-from-env"
