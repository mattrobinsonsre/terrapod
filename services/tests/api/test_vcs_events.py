"""Services-API tests for the GitHub webhook receiver (`routers/vcs_events.py`).

This is a publicly-reachable, unauthenticated endpoint whose only defense is
HMAC-SHA256 signature validation + an installation allow-list. Its helper
(`validate_webhook_signature`) was unit-tested in isolation, but the router
itself — reject-on-bad-signature, ping handshake, enqueue-on-valid-push,
unknown-installation rejection, malformed-body handling — had no coverage.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.config import settings

_BASE = "http://test"
_PUSH = {
    "repository": {"full_name": "acme/infra"},
    "installation": {"id": 4242},
}


@pytest.fixture(autouse=True)
def _webhook_secret():
    original = settings.vcs.github.webhook_secret
    settings.vcs.github.webhook_secret = "shhh"
    yield
    settings.vcs.github.webhook_secret = original


def _app():
    return create_app()


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestGithubWebhook:
    async def test_not_configured_returns_404(self, *_mocks):
        settings.vcs.github.webhook_secret = ""  # disable
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-events/github", content=b"{}")
        assert resp.status_code == 404

    @patch("terrapod.api.routers.vcs_events.validate_webhook_signature", return_value=False)
    async def test_invalid_signature_rejected_401(self, _sig, *_mocks):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=b"{}",
                headers={"X-Hub-Signature-256": "sha256=bogus"},
            )
        assert resp.status_code == 401

    @patch("terrapod.api.routers.vcs_events.validate_webhook_signature", return_value=True)
    async def test_ping_acks_pong(self, _sig, *_mocks):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=b"{}",
                headers={"X-GitHub-Event": "ping"},
            )
        assert resp.status_code == 200
        assert resp.json()["message"] == "pong"

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_connection", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events.validate_webhook_signature", return_value=True)
    async def test_valid_push_enqueues_immediate_poll(
        self, _sig, mock_resolve, mock_enqueue, *_mocks
    ):
        mock_resolve.return_value = MagicMock()  # known installation
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=json.dumps(_PUSH).encode(),
                headers={"X-GitHub-Event": "push"},
            )
        assert resp.status_code == 200
        triggers = [call.args[0] for call in mock_enqueue.await_args_list]
        assert "vcs_immediate_poll" in triggers

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_connection", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events.validate_webhook_signature", return_value=True)
    async def test_unknown_installation_does_not_enqueue(
        self, _sig, mock_resolve, mock_enqueue, *_mocks
    ):
        mock_resolve.return_value = None  # unknown installation
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=json.dumps(_PUSH).encode(),
                headers={"X-GitHub-Event": "push"},
            )
        # 200 so GitHub stops retrying, but no work enqueued.
        assert resp.status_code == 200
        assert resp.json()["message"] == "unknown installation"
        mock_enqueue.assert_not_awaited()

    @patch("terrapod.api.routers.vcs_events.validate_webhook_signature", return_value=True)
    async def test_malformed_json_returns_400(self, _sig, *_mocks):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=b"not json{{{",
                headers={"X-GitHub-Event": "push"},
            )
        assert resp.status_code == 400

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_connection", new_callable=AsyncMock)
    async def test_per_connection_secret_takes_precedence(
        self, mock_resolve, mock_enqueue, *_mocks
    ):
        """A connection with its own webhook secret validates against THAT
        secret, not the global one — using the REAL signature validator."""
        import hashlib
        import hmac

        conn = MagicMock()
        conn.webhook_secret = "per-conn-secret"
        mock_resolve.return_value = conn
        payload = json.dumps(_PUSH).encode()

        # Signed with the per-connection secret → accepted.
        good = "sha256=" + hmac.new(b"per-conn-secret", payload, hashlib.sha256).hexdigest()
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=payload,
                headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": good},
            )
        assert resp.status_code == 200

        # Signed with the GLOBAL secret → rejected, because the per-connection
        # secret takes precedence and the global signature won't match it.
        bad = "sha256=" + hmac.new(b"shhh", payload, hashlib.sha256).hexdigest()
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=payload,
                headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": bad},
            )
        assert resp.status_code == 401

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_connection", new_callable=AsyncMock)
    async def test_falls_back_to_global_secret(self, mock_resolve, mock_enqueue, *_mocks):
        """A connection WITHOUT its own secret validates against the global
        one (REAL validator)."""
        import hashlib
        import hmac

        conn = MagicMock()
        conn.webhook_secret = None  # no per-connection secret
        mock_resolve.return_value = conn
        payload = json.dumps(_PUSH).encode()

        good = "sha256=" + hmac.new(b"shhh", payload, hashlib.sha256).hexdigest()
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/github",
                content=payload,
                headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": good},
            )
        assert resp.status_code == 200


_GL_PUSH = {
    "project": {
        "path_with_namespace": "acme/infra",
        "git_http_url": "https://gitlab.com/acme/infra.git",
    }
}


@pytest.fixture
def _gitlab_secret():
    original = settings.vcs.gitlab.webhook_secret
    settings.vcs.gitlab.webhook_secret = "glsecret"
    yield
    settings.vcs.gitlab.webhook_secret = original


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@pytest.mark.usefixtures("_gitlab_secret")
class TestGitlabWebhook:
    """GitLab receiver (#590): X-Gitlab-Token secret, not HMAC signature."""

    async def test_not_configured_returns_404(self, *_mocks):
        settings.vcs.gitlab.webhook_secret = ""  # disable; resolver finds no conn
        with patch(
            "terrapod.api.routers.vcs_events._resolve_gitlab_connection",
            new_callable=AsyncMock,
            return_value=None,
        ):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                resp = await c.post(
                    "/api/terrapod/v1/vcs-events/gitlab",
                    content=json.dumps(_GL_PUSH).encode(),
                    headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "x"},
                )
        assert resp.status_code == 404

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_gitlab_connection", new_callable=AsyncMock)
    async def test_invalid_token_rejected_401(self, mock_resolve, _enq, *_mocks):
        conn = MagicMock()
        conn.webhook_secret = None  # falls back to the global "glsecret"
        mock_resolve.return_value = conn
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/gitlab",
                content=json.dumps(_GL_PUSH).encode(),
                headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "wrong"},
            )
        assert resp.status_code == 401

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_gitlab_connection", new_callable=AsyncMock)
    async def test_valid_push_enqueues_immediate_poll(self, mock_resolve, mock_enqueue, *_mocks):
        conn = MagicMock()
        conn.webhook_secret = None  # validate against global "glsecret"
        mock_resolve.return_value = conn
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/gitlab",
                content=json.dumps(_GL_PUSH).encode(),
                headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "glsecret"},
            )
        assert resp.status_code == 200
        calls = [(c.args[0], c.args[1]) for c in mock_enqueue.await_args_list]
        assert ("vcs_immediate_poll", {"repo": "acme/infra", "provider": "gitlab"}) in calls

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_gitlab_connection", new_callable=AsyncMock)
    async def test_unknown_project_does_not_enqueue(self, mock_resolve, mock_enqueue, *_mocks):
        mock_resolve.return_value = None  # no matching connection
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/gitlab",
                content=json.dumps(_GL_PUSH).encode(),
                # valid global token so we reach the unknown-project branch
                headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "glsecret"},
            )
        assert resp.status_code == 200
        assert resp.json()["message"] == "unknown project"
        mock_enqueue.assert_not_awaited()

    @patch("terrapod.api.routers.vcs_events._resolve_gitlab_connection", new_callable=AsyncMock)
    async def test_malformed_json_returns_400(self, mock_resolve, *_mocks):
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/gitlab",
                content=b"not json{{{",
                headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "glsecret"},
            )
        assert resp.status_code == 400

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_gitlab_connection", new_callable=AsyncMock)
    async def test_per_connection_secret_takes_precedence(self, mock_resolve, _enq, *_mocks):
        conn = MagicMock()
        conn.webhook_secret = "per-conn"
        mock_resolve.return_value = conn
        body = json.dumps(_GL_PUSH).encode()
        # The per-connection token is accepted.
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            ok = await c.post(
                "/api/terrapod/v1/vcs-events/gitlab",
                content=body,
                headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "per-conn"},
            )
        assert ok.status_code == 200
        # The GLOBAL token is rejected — the per-connection secret takes precedence.
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            bad = await c.post(
                "/api/terrapod/v1/vcs-events/gitlab",
                content=body,
                headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "glsecret"},
            )
        assert bad.status_code == 401

    @patch("terrapod.api.routers.vcs_events.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.routers.vcs_events._resolve_gitlab_connection", new_callable=AsyncMock)
    async def test_ignored_event_type_does_not_enqueue(self, mock_resolve, mock_enqueue, *_mocks):
        conn = MagicMock()
        conn.webhook_secret = None
        mock_resolve.return_value = conn
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/vcs-events/gitlab",
                content=json.dumps(_GL_PUSH).encode(),
                headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "glsecret"},
            )
        assert resp.status_code == 200
        mock_enqueue.assert_not_awaited()


class TestUrlHost:
    def test_extracts_host(self):
        from terrapod.api.routers.vcs_events import _url_host

        assert _url_host("https://gitlab.com/acme/infra.git") == "gitlab.com"
        assert _url_host("https://gitlab.example.com/g/p") == "gitlab.example.com"

    def test_bare_host_and_empty(self):
        from terrapod.api.routers.vcs_events import _url_host

        assert _url_host("gitlab.com") == "gitlab.com"
        assert _url_host("") == ""


class TestResolveGitlabConnection:
    """Host-binds an incoming GitLab webhook to a configured connection."""

    def _db_returning(self, conns):
        from contextlib import asynccontextmanager

        result = MagicMock()
        result.scalars.return_value.all.return_value = conns
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)

        @asynccontextmanager
        async def _ctx():
            yield db

        return _ctx

    async def test_single_host_match_returned(self):
        from terrapod.api.routers import vcs_events

        conn = MagicMock(server_url="", webhook_secret=None)  # gitlab.com default
        with patch.object(vcs_events, "get_db_session", self._db_returning([conn])):
            got = await vcs_events._resolve_gitlab_connection(
                "https://gitlab.com/acme/infra.git", "tok"
            )
        assert got is conn

    async def test_no_host_match_returns_none(self):
        from terrapod.api.routers import vcs_events

        conn = MagicMock(server_url="https://gitlab.example.com", webhook_secret=None)
        with patch.object(vcs_events, "get_db_session", self._db_returning([conn])):
            got = await vcs_events._resolve_gitlab_connection(
                "https://gitlab.com/acme/infra.git", "tok"
            )
        assert got is None

    async def test_multiple_same_host_disambiguated_by_secret(self):
        from terrapod.api.routers import vcs_events

        a = MagicMock(server_url="", webhook_secret="aaa")
        b = MagicMock(server_url="", webhook_secret="bbb")
        with patch.object(vcs_events, "get_db_session", self._db_returning([a, b])):
            got = await vcs_events._resolve_gitlab_connection(
                "https://gitlab.com/acme/infra.git", "bbb"
            )
        assert got is b
