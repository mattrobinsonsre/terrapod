"""Tests for the Slack account-linking API (#556)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_LINK = "/api/terrapod/v1/slack/link"


def _user(email="alice@example.com"):
    return AuthenticatedUser(
        email=email,
        display_name="Alice",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _fake_link(email="alice@example.com"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        slack_team_id="T123",
        slack_user_id="U456",
        terrapod_email=email,
        linked_via="slash_command",
        linked_at=datetime(2026, 7, 3, tzinfo=UTC),
    )


class TestLinkAccount:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_missing_state_422(self, *_m):
        app, _db = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_LINK, json={}, headers=_AUTH)
        assert r.status_code == 422

    @patch("terrapod.services.slack_link_service.verify_and_consume_state")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_bad_state_400(self, _idb, _ir, _is, verify):
        from terrapod.services.slack_link_service import LinkStateError

        verify.side_effect = LinkStateError("link state already used or expired")
        app, _db = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_LINK, json={"state": "bad"}, headers=_AUTH)
        assert r.status_code == 400

    @patch("terrapod.services.slack_link_service.create_link", new_callable=AsyncMock)
    @patch("terrapod.services.slack_link_service.verify_and_consume_state")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_link_happy_binds_current_user(self, _idb, _ir, _is, verify, create):
        verify.return_value = ("T123", "U456")
        create.return_value = _fake_link("alice@example.com")
        app, _db = _make_app(_user("alice@example.com"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_LINK, json={"state": "good"}, headers=_AUTH)
        assert r.status_code == 200
        attrs = r.json()["data"]
        assert attrs["email"] == "alice@example.com"
        assert attrs["slack-team-id"] == "T123"
        # The binding is attributed to the AUTHENTICATED user, not the payload.
        _args, kwargs = create.call_args
        assert kwargs["email"] == "alice@example.com"
