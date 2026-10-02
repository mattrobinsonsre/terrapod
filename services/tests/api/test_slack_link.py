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
        verify.return_value = ("T123", "U456", "")
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


class TestPreviewLink:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_missing_state_422(self, *_m):
        app, _db = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(f"{_LINK}/preview", json={}, headers=_AUTH)
        assert r.status_code == 422

    @patch("terrapod.services.slack_link_service.peek_link_state", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_bad_state_400(self, _idb, _ir, _is, peek):
        from terrapod.services.slack_link_service import LinkStateError

        peek.side_effect = LinkStateError("link state already used or expired")
        app, _db = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(f"{_LINK}/preview", json={"state": "bad"}, headers=_AUTH)
        assert r.status_code == 400

    @patch("terrapod.services.slack_link_service.peek_link_state", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_preview_describes_identity_against_caller(self, _idb, _ir, _is, peek):
        """Preview shows WHICH Slack identity would bind + the caller's email, and
        does NOT consume the state (that only happens on confirm)."""
        peek.return_value = ("T123", "U456")
        app, _db = _make_app(_user("alice@example.com"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(f"{_LINK}/preview", json={"state": "good"}, headers=_AUTH)
        assert r.status_code == 200
        attrs = r.json()["data"]
        assert attrs["slack-team-id"] == "T123"
        assert attrs["slack-user-id"] == "U456"
        assert attrs["email"] == "alice@example.com"


class TestListLinks:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_returns_callers_links(self, *_m):
        app, db = _make_app(_user("alice@example.com"))
        result = MagicMock()
        result.scalars.return_value.all.return_value = [_fake_link("alice@example.com")]
        db.execute = AsyncMock(return_value=result)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.get("/api/terrapod/v1/slack/links", headers=_AUTH)
        assert r.status_code == 200
        assert r.json()["data"][0]["email"] == "alice@example.com"


class TestUnlink:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_unlink_own_link_deletes(self, *_m):
        app, db = _make_app(_user("alice@example.com"))
        link = _fake_link("alice@example.com")
        db.get = AsyncMock(return_value=link)
        db.delete = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.delete(f"/api/terrapod/v1/slack/links/slk-{link.id}", headers=_AUTH)
        assert r.status_code == 204
        db.delete.assert_awaited_once_with(link)

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_unlink_someone_elses_link_404_no_delete(self, *_m):
        """Ownership gate: a user cannot unlink another user's binding."""
        app, db = _make_app(_user("alice@example.com"))
        link = _fake_link("bob@example.com")  # owned by someone else
        db.get = AsyncMock(return_value=link)
        db.delete = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.delete(f"/api/terrapod/v1/slack/links/slk-{link.id}", headers=_AUTH)
        assert r.status_code == 404
        db.delete.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_unlink_malformed_id_404(self, *_m):
        app, db = _make_app(_user())
        db.get = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.delete("/api/terrapod/v1/slack/links/not-a-uuid", headers=_AUTH)
        assert r.status_code == 404
        db.get.assert_not_awaited()


class TestTheConfirmScreenNamesARecognisableHuman:
    """GHSA-5899-fm2p-88x3. The attack is a confused deputy: an attacker mints a
    perfectly valid state for their OWN Slack identity and sends the URL to a victim,
    whose authenticated session completes the bind. The deliberate Confirm click does
    not stop that — the victim has to be able to tell the identity is not theirs, and
    `U04F2AB3C` does not let them.

    So these assert on the NAMES, not on the endpoint returning 200: the endpoint
    returned 200 before the fix too.
    """

    @patch("terrapod.services.slack_link_service.describe_slack_identity", new_callable=AsyncMock)
    @patch("terrapod.services.slack_link_service.peek_link_state", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_preview_returns_the_handle_real_name_and_team_name(
        self, _idb, _ir, _is, peek, describe
    ):
        peek.return_value = ("T123", "U456")
        describe.return_value = {
            "user-name": "dave",
            "user-real-name": "Dave Smith",
            "team-name": "Acme Corp",
            "resolved": "true",
        }
        app, _db = _make_app(_user("alice@example.com"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(f"{_LINK}/preview", json={"state": "good"}, headers=_AUTH)
        assert r.status_code == 200
        attrs = r.json()["data"]
        assert attrs["user-real-name"] == "Dave Smith"
        assert attrs["user-name"] == "dave"
        assert attrs["team-name"] == "Acme Corp"
        assert attrs["resolved"] == "true"
        # The ids stay as corroboration rather than being replaced: someone who DOES
        # know their own Slack id should still be able to check it.
        assert attrs["slack-user-id"] == "U456"

    @patch("terrapod.services.slack_link_service.describe_slack_identity", new_callable=AsyncMock)
    @patch("terrapod.services.slack_link_service.peek_link_state", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_a_failed_lookup_says_so_and_does_not_block_linking(
        self, _idb, _ir, _is, peek, describe
    ):
        """`users:read` may not be granted, and Slack may be having a bad day.
        Refusing the link then would be a worse outcome than an unnamed screen — but
        `resolved: false` has to be explicit, or the page cannot tell an absent
        display name from a failed lookup and would present ids as if chosen."""
        peek.return_value = ("T123", "U456")
        describe.return_value = {
            "user-name": "",
            "user-real-name": "",
            "team-name": "",
            "resolved": "false",
        }
        app, _db = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(f"{_LINK}/preview", json={"state": "good"}, headers=_AUTH)
        assert r.status_code == 200
        assert r.json()["data"]["resolved"] == "false"


class TestABindingLeavesATrail:
    """A binding is a standing ability to act as an account from Slack, and it left
    no record at all — so "who attached that Slack account to the admin" had no
    answer. Terrapod has no per-user notification channel, so the audit row IS the
    trail; inventing an email path for this would be a bigger change than the
    finding warrants.
    """

    @patch("terrapod.api.routers.slack.log_audit_event", new_callable=AsyncMock)
    @patch("terrapod.services.slack_link_service.describe_slack_identity", new_callable=AsyncMock)
    @patch("terrapod.services.slack_link_service.create_link", new_callable=AsyncMock)
    @patch("terrapod.services.slack_link_service.verify_and_consume_state", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_creating_a_link_is_audited_with_who_was_bound_to_what(
        self, _idb, _ir, _is, verify, create, describe, audit
    ):
        verify.return_value = ("T123", "U456", "")
        create.return_value = _fake_link("alice@example.com")
        describe.return_value = {
            "user-name": "dave",
            "user-real-name": "Dave Smith",
            "team-name": "Acme Corp",
            "resolved": "true",
        }
        app, _db = _make_app(_user("alice@example.com"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_LINK, json={"state": "good"}, headers=_AUTH)
        assert r.status_code == 200
        assert audit.await_count == 1
        kw = audit.await_args.kwargs
        assert kw["action"] == "slack.link.create"
        assert kw["actor_email"] == "alice@example.com"
        # Both halves are in the detail: a row naming only the opaque id leaves the
        # reader doing the lookup the fix exists to do for them.
        assert "U456" in kw["detail"] and "dave" in kw["detail"]
        assert "Acme Corp" in kw["detail"]

    @patch("terrapod.api.routers.slack.log_audit_event", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_revoking_a_link_is_audited(self, _idb, _ir, _is, audit):
        """ "The link is gone" and "the link was never there" are different answers to
        the same question, and only a row distinguishes them."""
        link = _fake_link("alice@example.com")
        db = AsyncMock()
        db.add = MagicMock()
        db.get = AsyncMock(return_value=link)
        app, _db = _make_app(_user("alice@example.com"), mock_db=db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.delete(f"/api/terrapod/v1/slack/links/slk-{link.id}", headers=_AUTH)
        assert r.status_code == 204
        assert audit.await_count == 1
        kw = audit.await_args.kwargs
        assert kw["action"] == "slack.link.revoke"
        assert "U456" in kw["detail"]


class TestTheIdentityLookupDegradesRatherThanRaising:
    async def test_it_returns_unresolved_when_slack_is_disabled(self):
        """An operator who has not enabled Slack still has the endpoints mounted, and
        a lookup that raised would turn a missing integration into a 500."""
        from terrapod.config import settings
        from terrapod.services.slack_link_service import describe_slack_identity

        cfg = settings.slack
        old = cfg.enabled
        try:
            cfg.enabled = False
            out = await describe_slack_identity("T1", "U1")
        finally:
            cfg.enabled = old
        assert out["resolved"] == "false" and out["user-name"] == ""

    async def test_a_raising_slack_client_is_not_fatal(self):
        """A missing `users:read` scope presents as an exception from users.info.
        Blocking the link on it would make a scope the operator may not control into
        a hard dependency of account linking."""
        from terrapod.config import settings
        from terrapod.services import slack_link_service

        cfg = settings.slack
        old_enabled, old_token = cfg.enabled, cfg.bot_token
        try:
            cfg.enabled, cfg.bot_token = True, "xoxb-test"
            client = MagicMock()
            client.users_info = AsyncMock(side_effect=RuntimeError("missing_scope"))
            with patch.object(slack_link_service, "_bot_client", return_value=client):
                out = await slack_link_service.describe_slack_identity("T1", "U1")
        finally:
            cfg.enabled, cfg.bot_token = old_enabled, old_token
        assert out["resolved"] == "false"
