"""Tests for the bulk workspace operations router (#318, admin-only).

Covers `POST /api/terrapod/v1/workspaces/actions/search` and
`POST /api/terrapod/v1/workspaces/actions/bulk-update`, including the
zero-mutation validation contract, the dry-run/apply transaction
guarantee, run-task upsert, failure rollback, and pool RBAC.

Harness mirrors `test_vcs_connections.py` / `test_autodiscovery_rules.py`.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.auth.capabilities import caps_for_level
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


def _non_admin():
    """Passes the `require_admin` override (tests inject it) but carries
    no `admin` role, so the in-handler pool-write check actually runs."""
    return AuthenticatedUser(
        email="dev@example.com",
        display_name="Dev",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _mock_ws(
    ws_id=None,
    name="prod-net",
    execution_mode="agent",
    execution_backend="tofu",
    terraform_version="1.12",
    agent_pool_id=None,
    labels=None,
    auto_apply=False,
    var_files=None,
):
    w = MagicMock()
    w.id = ws_id or uuid.uuid4()
    w.name = name
    w.execution_mode = execution_mode
    w.execution_backend = execution_backend
    w.terraform_version = terraform_version
    w.agent_pool_id = agent_pool_id
    w.labels = labels if labels is not None else {}
    w.auto_apply = auto_apply
    w.var_files = var_files if var_files is not None else []
    return w


def _list_result(values):
    """A SQLAlchemy result-like whose scalars().all() returns `values`."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


def _scalar_result(value):
    """A SQLAlchemy result-like whose scalar_one_or_none() returns `value`."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


# ── search ───────────────────────────────────────────────────────────────


class TestSearch:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_matched_list_shape(self, *_mocks):
        wss = [_mock_ws(name="a"), _mock_ws(name="b")]
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result(wss))

        body = {"filter": {"execution-backend": "tofu"}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/search", json=body, headers=_AUTH
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["matched"] == 2
        assert {w["name"] for w in data["workspaces"]} == {"a", "b"}
        first = data["workspaces"][0]
        assert first["id"].startswith("ws-")
        assert "execution-backend" in first
        assert "labels" in first

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_empty_filter_422(self, *_mocks):
        app, _db = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/search", json={}, headers=_AUTH
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_no_dimensions_422(self, *_mocks):
        app, _db = _make_app(_admin())
        body = {"filter": {}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/search", json=body, headers=_AUTH
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_all_true_returns_everything(self, *_mocks):
        wss = [_mock_ws(name="a")]
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result(wss))
        body = {"filter": {"all": True}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/search", json=body, headers=_AUTH
            )
        assert resp.status_code == 200
        assert resp.json()["matched"] == 1


# ── bulk-update: validation (zero mutation) ──────────────────────────────


class TestBulkUpdateValidation:
    async def _post(self, body, db_setup=None, user=None):
        app, db = _make_app(user or _admin())
        db.execute = AsyncMock(return_value=_list_result([]))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        if db_setup:
            db_setup(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        return resp, db

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_bad_execution_backend_422_no_commit(self, *_mocks):
        body = {
            "filter": {"all": True},
            "update": {"execution-backend": "puppet"},
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "execution-backend" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_bad_execution_mode_422(self, *_mocks):
        body = {"filter": {"all": True}, "update": {"execution-mode": "remote"}}
        resp, db = await self._post(body)
        assert resp.status_code == 422
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_reserved_label_key_422(self, *_mocks):
        """#316 chokepoint: a reserved label key in update.labels is
        rejected before any mutation."""
        body = {
            "filter": {"all": True},
            "update": {"labels": {"owner": "x", "team": "y"}},
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "owner" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_run_task_missing_url_422(self, *_mocks):
        body = {
            "filter": {"all": True},
            "update": {
                "run-tasks": [{"name": "scan", "stage": "pre_plan"}],
            },
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "url is required" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_run_task_bad_stage_422(self, *_mocks):
        body = {
            "filter": {"all": True},
            "update": {
                "run-tasks": [{"name": "scan", "url": "https://x", "stage": "post_apply"}],
            },
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "stage" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_run_task_bad_enforcement_422(self, *_mocks):
        body = {
            "filter": {"all": True},
            "update": {
                "run-tasks": [
                    {
                        "name": "scan",
                        "url": "https://x",
                        "stage": "pre_plan",
                        "enforcement-level": "blocking",
                    }
                ],
            },
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "enforcement-level" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_notification_bad_destination_type_422(self, *_mocks):
        body = {
            "filter": {"all": True},
            "update": {
                "notification-configurations": [
                    {"name": "n1", "destination-type": "carrier-pigeon"}
                ],
            },
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "destination-type" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_notification_bad_trigger_422(self, *_mocks):
        body = {
            "filter": {"all": True},
            "update": {
                "notification-configurations": [
                    {
                        "name": "n1",
                        "destination-type": "slack",
                        "triggers": ["run:exploded"],
                    }
                ],
            },
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "invalid triggers" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_empty_update_422(self, *_mocks):
        body = {"filter": {"all": True}, "update": {}}
        resp, db = await self._post(body)
        assert resp.status_code == 422
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_update_with_no_recognised_keys_422(self, *_mocks):
        body = {"filter": {"all": True}, "update": {"not-a-field": 1}}
        resp, db = await self._post(body)
        assert resp.status_code == 422
        assert "no recognised keys" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_unknown_filter_422(self, *_mocks):
        body = {
            "filter": {"bogus-selector": "x"},
            "update": {"execution-backend": "tofu"},
        }
        resp, db = await self._post(body)
        assert resp.status_code == 422
        db.commit.assert_not_awaited()


# ── bulk-update: dry-run ─────────────────────────────────────────────────


class TestBulkUpdateDryRun:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_dry_run_default_rolls_back(self, _db, _redis, _storage):
        """`dry_run` defaults to true: the diff is computed, rollback is
        called, commit is NOT — the no-side-effect guarantee. Audit rows
        are added into the same transaction (no internal commit), so they
        roll back too; nothing persists."""
        ws = _mock_ws(name="w1", execution_backend="terraform")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result([ws]))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        body = {"filter": {"all": True}, "update": {"execution-backend": "tofu"}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dry_run"] is True
        assert data["matched"] == 1
        assert len(data["would_change"]) == 1
        diff = data["would_change"][0]["diff"]
        assert diff["execution_backend"] == {"from": "terraform", "to": "tofu"}
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_dry_run_true_explicit(self, _db, _redis, _storage):
        ws = _mock_ws(name="w1", terraform_version="1.10")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result([ws]))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        body = {
            "filter": {"all": True},
            "update": {"terraform-version": "1.12"},
            "dry_run": True,
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True
        db.rollback.assert_awaited()
        db.commit.assert_not_awaited()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_dry_run_no_change_lists_unchanged(self, *_mocks):
        """A workspace already at the target value lands in `unchanged`,
        with an empty `would_change`."""
        ws = _mock_ws(name="w1", execution_backend="tofu")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result([ws]))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        body = {"filter": {"all": True}, "update": {"execution-backend": "tofu"}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["would_change"] == []
        assert len(data["unchanged"]) == 1
        db.commit.assert_not_awaited()


# ── bulk-update: apply ───────────────────────────────────────────────────


class TestBulkUpdateApply:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_apply_commits_and_mutates(self, _db, _redis, _storage):
        ws = _mock_ws(name="w1", execution_backend="terraform")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result([ws]))
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        body = {
            "filter": {"all": True},
            "update": {"execution-backend": "tofu"},
            "dry_run": False,
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["dry_run"] is False
        assert data["applied"] == 1
        assert len(data["changes"]) == 1
        # The ORM object was actually mutated.
        assert ws.execution_backend == "tofu"
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
        # The audit row is added into the same transaction (not via the
        # committing helper) — assert one was queued for this change.
        assert any(
            getattr(c.args[0], "action", None) == "workspace.bulk_update"
            for c in db.add.call_args_list
        )

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_run_task_inserted_when_absent(self, _db, _redis, _storage):
        """Apply with a run-task spec and NO existing RunTask → db.add()
        is called with a freshly built RunTask."""
        ws = _mock_ws(name="w1")
        app, db = _make_app(_admin())
        # 1st execute: workspace selection; 2nd: RunTask upsert lookup → none.
        db.execute = AsyncMock(side_effect=[_list_result([ws]), _scalar_result(None)])
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        body = {
            "filter": {"all": True},
            "update": {"run-tasks": [{"name": "scan", "url": "https://x", "stage": "pre_plan"}]},
            "dry_run": False,
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        # db.add is also called for the per-workspace AuditLog row — scope
        # the assertion to the RunTask that was inserted.
        added = [c.args[0] for c in db.add.call_args_list if type(c.args[0]).__name__ == "RunTask"]
        assert len(added) == 1
        assert added[0].name == "scan"
        assert added[0].workspace_id == ws.id
        db.commit.assert_awaited_once()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_run_task_mutated_when_present(self, _db, _redis, _storage):
        """Apply with a run-task spec and an EXISTING RunTask → the
        existing row's attributes are set in place; no *RunTask* is
        added (an AuditLog row still is)."""
        ws = _mock_ws(name="w1")
        existing_rt = MagicMock()
        existing_rt.name = "scan"
        existing_rt.url = "https://old"
        app, db = _make_app(_admin())
        db.execute = AsyncMock(side_effect=[_list_result([ws]), _scalar_result(existing_rt)])
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        body = {
            "filter": {"all": True},
            "update": {"run-tasks": [{"name": "scan", "url": "https://new", "stage": "pre_plan"}]},
            "dry_run": False,
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert not any(type(c.args[0]).__name__ == "RunTask" for c in db.add.call_args_list)
        assert existing_rt.url == "https://new"
        assert existing_rt.stage == "pre_plan"
        db.commit.assert_awaited_once()


# ── bulk-update: failure rollback ────────────────────────────────────────


class TestBulkUpdateFailureRollback:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_apply_failure_rolls_back_409(self, _db, _redis, _storage):
        """If the apply transaction fails (here: the single commit raises)
        the endpoint returns 409 and rolls back — nothing is left
        partially applied."""
        ws = _mock_ws(name="w1", execution_backend="terraform")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result([ws]))
        db.commit = AsyncMock(side_effect=RuntimeError("db down"))
        db.rollback = AsyncMock()
        body = {
            "filter": {"all": True},
            "update": {"execution-backend": "tofu"},
            "dry_run": False,
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 409, resp.text
        assert "rolled back" in resp.json()["detail"]
        db.rollback.assert_awaited()
        db.commit.assert_awaited()  # it was attempted, raised, then rolled back


# ── bulk-update: pool RBAC ───────────────────────────────────────────────


class TestBulkUpdatePoolRbac:
    @patch(
        "terrapod.api.routers.workspace_bulk.resolve_capabilities",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_admin_without_pool_write_403(self, _db, _redis, _storage, m_resolve):
        """A non-admin assigning an agent pool needs pool `write`; without
        it the update is rejected 403 with zero mutation."""
        pool = MagicMock()
        pool.id = uuid.uuid4()
        pool.name = "aws-prod"
        pool.labels = {}
        pool.owner_email = "someone-else@example.com"
        # `read` caps do NOT include pool:assign → the gate rejects.
        m_resolve.return_value = caps_for_level("read")

        app, db = _make_app(_non_admin())
        db.get = AsyncMock(return_value=pool)
        db.execute = AsyncMock(return_value=_list_result([]))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        body = {
            "filter": {"all": True},
            "update": {"agent-pool-id": f"apool-{pool.id}"},
            "dry_run": False,
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 403, resp.text
        assert "write permission on agent pool" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @patch(
        "terrapod.api.routers.workspace_bulk.resolve_capabilities",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_admin_with_pool_write_allowed(self, _db, _redis, _storage, m_resolve):
        pool = MagicMock()
        pool.id = uuid.uuid4()
        pool.name = "aws-prod"
        pool.labels = {}
        pool.owner_email = "someone-else@example.com"
        # `write` caps include pool:assign → the gate passes.
        m_resolve.return_value = caps_for_level("write")

        ws = _mock_ws(name="w1", agent_pool_id=None)
        app, db = _make_app(_non_admin())
        db.get = AsyncMock(return_value=pool)
        db.execute = AsyncMock(return_value=_list_result([ws]))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        body = {
            "filter": {"all": True},
            "update": {"agent-pool-id": f"apool-{pool.id}"},
            "dry_run": False,
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/workspaces/actions/bulk-update",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert ws.agent_pool_id == pool.id
        db.commit.assert_awaited_once()
