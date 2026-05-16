"""Tests for autodiscovery rule CRUD endpoints (terrapod #283, admin-only)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _admin():
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _mock_rule(
    rule_id=None,
    name="monorepo",
    repo_url="https://github.com/example/repo",
    pattern="accounts/*/**/*.tf",
    ignore_patterns=None,
):
    r = MagicMock()
    r.id = rule_id or uuid.uuid4()
    r.name = name
    r.name_template = ""
    r.vcs_connection_id = uuid.uuid4()
    r.repo_url = repo_url
    r.branch = ""
    r.pattern = pattern
    r.ignore_patterns = ignore_patterns or ["modules/**"]
    r.enabled = True
    r.execution_mode = "agent"
    r.execution_backend = "tofu"
    r.agent_pool_id = None
    r.terraform_version = "1.11"
    r.resource_cpu = "1"
    r.resource_memory = "2Gi"
    r.auto_apply = False
    r.labels = {"env": "monorepo"}
    r.owner_email = "admin@example.com"
    r.created_at = datetime(2026, 5, 9, tzinfo=UTC)
    r.updated_at = datetime(2026, 5, 9, tzinfo=UTC)
    r.first_scan_at = None
    return r


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _query_result(value):
    """Wrap a value as a SQLAlchemy result-like object."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalars.return_value.all.return_value = value if isinstance(value, list) else []
    return result


# ── List ─────────────────────────────────────────────────────────────────


class TestListRules:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_returns_all_rules(self, *_mocks):
        rules = [_mock_rule(name="alpha"), _mock_rule(name="beta")]
        app, db = _make_app(_admin())
        db.execute.return_value = _query_result(rules)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/autodiscovery-rules", headers=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert {r["attributes"]["name"] for r in body["data"]} == {"alpha", "beta"}


# ── Create ───────────────────────────────────────────────────────────────


class TestCreateRule:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_201_with_valid_attrs(self, *_mocks):
        conn_id = uuid.uuid4()
        # validate_connection: connection exists
        # refresh after commit
        app, db = _make_app(_admin())
        db.get = AsyncMock(side_effect=[MagicMock(id=conn_id)])  # _validate_connection
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        body = {
            "data": {
                "type": "autodiscovery-rules",
                "attributes": {
                    "name": "monorepo",
                    "vcs-connection-id": f"vcs-{conn_id}",
                    "repo-url": "https://github.com/example/repo",
                    "pattern": "accounts/*/**/*.tf",
                    "ignore-patterns": ["modules/**"],
                    "execution-mode": "agent",
                    "labels": {"env": "monorepo"},
                },
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/autodiscovery-rules", json=body, headers=_AUTH)
        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["name"] == "monorepo"
        assert attrs["pattern"] == "accounts/*/**/*.tf"
        assert attrs["ignore-patterns"] == ["modules/**"]
        assert attrs["execution-mode"] == "agent"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_when_pattern_missing(self, *_mocks):
        app, _db = _make_app(_admin())
        body = {
            "data": {
                "attributes": {
                    "name": "monorepo",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "repo-url": "https://github.com/example/repo",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/autodiscovery-rules", json=body, headers=_AUTH)
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_reserved_label_key(self, *_mocks):
        """#316: a reserved label key on the rule is rejected at the
        source — otherwise every workspace it materialises carries the
        key and becomes uneditable via PATCH.
        """
        app, _db = _make_app(_admin())
        body = {
            "data": {
                "attributes": {
                    "name": "monorepo",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "repo-url": "https://github.com/example/repo",
                    "pattern": "accounts/*/**/*.tf",
                    "execution-mode": "agent",
                    "labels": {"owner": "foundations", "team": "x"},
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/autodiscovery-rules", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "owner" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_when_connection_does_not_exist(self, *_mocks):
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=None)  # connection missing
        body = {
            "data": {
                "attributes": {
                    "name": "monorepo",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "repo-url": "https://github.com/example/repo",
                    "pattern": "accounts/*/**/*.tf",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/autodiscovery-rules", json=body, headers=_AUTH)
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_when_pattern_ends_in_slash(self, *_mocks):
        """A trailing-slash pattern can never match a file path — reject
        at create time with a clear hint instead of silently no-opping
        (issue #309)."""
        app, _db = _make_app(_admin())
        body = {
            "data": {
                "attributes": {
                    "name": "monorepo",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "repo-url": "https://github.com/example/repo",
                    "pattern": "accounts/*/",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/autodiscovery-rules", json=body, headers=_AUTH)
        assert resp.status_code == 422
        body = resp.json()
        assert "ends in '/'" in body["detail"]
        # Hint mentions both alternatives so the user can pick.
        assert "accounts/*/*.tf" in body["detail"]
        assert "accounts/*/**" in body["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_when_ignore_pattern_ends_in_slash(self, *_mocks):
        """Same rule applies to entries in ignore-patterns."""
        app, _db = _make_app(_admin())
        body = {
            "data": {
                "attributes": {
                    "name": "monorepo",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "repo-url": "https://github.com/example/repo",
                    "pattern": "**/*.tf",
                    "ignore-patterns": ["modules/"],
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/autodiscovery-rules", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "ignore-patterns" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_invalid_execution_mode(self, *_mocks):
        app, db = _make_app(_admin())
        body = {
            "data": {
                "attributes": {
                    "name": "monorepo",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "repo-url": "https://github.com/example/repo",
                    "pattern": "**/*.tf",
                    "execution-mode": "local",  # autodiscovery is VCS-driven; only "agent" is allowed
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/autodiscovery-rules", json=body, headers=_AUTH)
        assert resp.status_code == 422


# ── Show ─────────────────────────────────────────────────────────────────


class TestShowRule:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_200_when_exists(self, *_mocks):
        rule = _mock_rule()
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=rule)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/terrapod/v1/autodiscovery-rules/{rule.id}", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["name"] == "monorepo"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_404_when_missing(self, *_mocks):
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/autodiscovery-rules/{uuid.uuid4()}", headers=_AUTH
            )
        assert resp.status_code == 404


# ── Update ───────────────────────────────────────────────────────────────


class TestUpdateRule:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_patches_pattern_and_enabled(self, *_mocks):
        rule = _mock_rule()
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=rule)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {
            "data": {
                "attributes": {
                    "pattern": "envs/*/**/*.tf",
                    "enabled": False,
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/autodiscovery-rules/{rule.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert rule.pattern == "envs/*/**/*.tf"
        assert rule.enabled is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_re_enable_clears_first_scan_at(self, *_mocks):
        """Flipping enabled false → true clears first_scan_at so the next
        poll cycle re-walks the repo (handles the 'disabled for a while,
        repo grew, now re-enabling' case from issue #309).
        """
        from datetime import UTC, datetime

        rule = _mock_rule()
        rule.enabled = False
        rule.first_scan_at = datetime(2026, 1, 1, tzinfo=UTC)
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=rule)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {"data": {"attributes": {"enabled": True}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/autodiscovery-rules/{rule.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert rule.enabled is True
        assert rule.first_scan_at is None

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_no_op_enable_preserves_first_scan_at(self, *_mocks):
        """If `enabled` was already True and the PATCH sets it to True
        again, first_scan_at must NOT be reset — only the false → true
        transition triggers a re-scan."""
        from datetime import UTC, datetime

        rule = _mock_rule()
        rule.enabled = True
        scanned = datetime(2026, 1, 1, tzinfo=UTC)
        rule.first_scan_at = scanned
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=rule)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {"data": {"attributes": {"enabled": True}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/autodiscovery-rules/{rule.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert rule.first_scan_at == scanned


# ── Delete ───────────────────────────────────────────────────────────────


class TestDeleteRule:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_204_on_success(self, *_mocks):
        rule = _mock_rule()
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=rule)
        db.delete = AsyncMock()
        db.commit = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/terrapod/v1/autodiscovery-rules/{rule.id}", headers=_AUTH)
        assert resp.status_code == 204
        db.delete.assert_awaited_once_with(rule)

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_404_when_missing(self, *_mocks):
        app, db = _make_app(_admin())
        db.get = AsyncMock(return_value=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/autodiscovery-rules/{uuid.uuid4()}", headers=_AUTH
            )
        assert resp.status_code == 404
