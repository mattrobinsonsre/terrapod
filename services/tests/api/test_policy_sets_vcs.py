"""Tests for VCS-sourced policy set router endpoints.

Covers:
- create_policy_set with source=vcs (happy path + validation)
- 409 guards: add/update/delete policy on a VCS-sourced set
- POST /policy-sets/{id}/actions/sync endpoint
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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


def _make_app(mock_db=None):
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: _admin()
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _mock_policy_set(source="vcs", **overrides):
    ps = MagicMock()
    ps.id = overrides.get("id", uuid.uuid4())
    ps.name = overrides.get("name", "sec-baseline")
    ps.description = ""
    ps.enforcement_level = "mandatory"
    ps.enabled = True
    ps.global_scope = True
    ps.allow_labels = {}
    ps.allow_names = []
    ps.deny_labels = {}
    ps.deny_names = []
    ps.source = source
    ps.vcs_connection_id = overrides.get("vcs_connection_id", uuid.uuid4())
    ps.vcs_repo_url = "https://github.com/org/policies"
    ps.vcs_branch = "main"
    ps.policy_path = "policies"
    ps.shared_evaluation = False
    ps.support_files = {}
    ps.vcs_last_commit_sha = "abc123"
    ps.vcs_last_synced_at = datetime(2026, 5, 28, tzinfo=UTC)
    ps.vcs_last_error = None
    ps.policies = []
    ps.created_by = "admin@example.com"
    ps.created_at = datetime(2026, 5, 28, tzinfo=UTC)
    ps.updated_at = datetime(2026, 5, 28, tzinfo=UTC)
    return ps


def _mock_policy(policy_set_id=None):
    p = MagicMock()
    p.id = uuid.uuid4()
    p.policy_set_id = policy_set_id or uuid.uuid4()
    p.name = "deny_s3"
    p.rego = "package terrapod\ndeny contains msg if { false }"
    p.created_at = datetime(2026, 5, 28, tzinfo=UTC)
    p.updated_at = datetime(2026, 5, 28, tzinfo=UTC)
    return p


def _scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _list_result(values):
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


# ── Create with source=vcs ───────────────────────────────────────────


class TestCreatePolicySetVCS:
    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_201_vcs_happy(self, *_mocks):
        app, db = _make_app()
        db.commit = AsyncMock()
        db.add = MagicMock()

        ps = _mock_policy_set(source="vcs")
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        body = {
            "data": {
                "type": "policy-sets",
                "attributes": {
                    "name": "sec-baseline",
                    "enforcement-level": "mandatory",
                    "source": "vcs",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "vcs-repo-url": "https://github.com/org/policies",
                    "vcs-branch": "main",
                    "policy-path": "policies",
                },
            }
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/policy-sets", json=body, headers=_AUTH)
        assert resp.status_code == 201

    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_404_vcs_connection_not_found(self, *_mocks):
        app, db = _make_app()
        db.commit = AsyncMock()
        db.add = MagicMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        body = {
            "data": {
                "type": "policy-sets",
                "attributes": {
                    "name": "sec-baseline",
                    "enforcement-level": "mandatory",
                    "source": "vcs",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                    "vcs-repo-url": "https://github.com/org/policies",
                    "vcs-branch": "main",
                    "policy-path": "policies",
                },
            }
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/policy-sets", json=body, headers=_AUTH)
        assert resp.status_code == 404
        assert "VCS connection" in resp.json()["detail"]

    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_vcs_missing_connection_id(self, *_mocks):
        app, db = _make_app()

        body = {
            "data": {
                "type": "policy-sets",
                "attributes": {
                    "name": "broken",
                    "source": "vcs",
                    "vcs-repo-url": "https://github.com/org/policies",
                },
            }
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/policy-sets", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "vcs-connection-id" in resp.json()["detail"]

    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_vcs_missing_repo_url(self, *_mocks):
        app, db = _make_app()

        body = {
            "data": {
                "type": "policy-sets",
                "attributes": {
                    "name": "broken",
                    "source": "vcs",
                    "vcs-connection-id": f"vcs-{uuid.uuid4()}",
                },
            }
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/policy-sets", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "vcs-repo-url" in resp.json()["detail"]


# ── Update guards ────────────────────────────────────────────────────


class TestUpdatePolicySetVCS:
    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_source_immutable(self, *_mocks):
        app, db = _make_app()
        ps = _mock_policy_set(source="vcs")
        db.execute = AsyncMock(return_value=_scalar_result(ps))
        db.commit = AsyncMock()

        body = {
            "data": {
                "type": "policy-sets",
                "attributes": {"source": "inline"},
            }
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "source" in resp.json()["detail"]


# ── 409 guards: inline CRUD on VCS-sourced sets ──────────────────────


class TestVCSPolicySet409Guards:
    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_add_policy_409_when_vcs(self, *_mocks):
        ps = _mock_policy_set(source="vcs")
        app, db = _make_app()
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        body = {
            "data": {
                "type": "policies",
                "attributes": {"name": "new-rule", "rego": "package terrapod\n"},
            }
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}/policies",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 409
        assert "VCS-sourced" in resp.json()["detail"]

    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_update_policy_409_when_vcs(self, *_mocks):
        ps = _mock_policy_set(source="vcs")
        policy = _mock_policy(policy_set_id=ps.id)
        app, db = _make_app()

        # First call returns the policy, second returns the policy set
        db.execute = AsyncMock(side_effect=[_scalar_result(policy), _scalar_result(ps)])

        body = {
            "data": {
                "type": "policies",
                "attributes": {"rego": "package terrapod\nupdated"},
            }
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/policies/pol-{policy.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 409
        assert "VCS-sourced" in resp.json()["detail"]

    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_policy_409_when_vcs(self, *_mocks):
        ps = _mock_policy_set(source="vcs")
        policy = _mock_policy(policy_set_id=ps.id)
        app, db = _make_app()

        db.execute = AsyncMock(side_effect=[_scalar_result(policy), _scalar_result(ps)])

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/policies/pol-{policy.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 409
        assert "VCS-sourced" in resp.json()["detail"]


# ── Sync endpoint ────────────────────────────────────────────────────


class TestSyncPolicySetEndpoint:
    @pytest.mark.asyncio
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_202_vcs_set(self, _init_db, _init_redis, _init_storage, mock_enqueue):
        ps = _mock_policy_set(source="vcs")
        app, db = _make_app()
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}/actions/sync",
                headers=_AUTH,
            )
        assert resp.status_code == 202
        mock_enqueue.assert_called_once()

    @pytest.mark.asyncio
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_409_inline_set(self, *_mocks):
        ps = _mock_policy_set(source="inline")
        app, db = _make_app()
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}/actions/sync",
                headers=_AUTH,
            )
        assert resp.status_code == 409
        assert "VCS-sourced" in resp.json()["detail"]


class TestSwitchingSharedEvaluationOnReReadsTheRepository:
    """The as-code path has to force a sync, exactly as the Sync button does.

    `support_files` is only ever populated by a sync, and the poller returns
    early when the branch head has not moved -- which it has not, for every
    set that already existed when the deployment upgraded. So a set switched
    to shared evaluation without a forced re-read evaluates with NO data
    files, and a rule reading `data.<key>` is *undefined* rather than failing:
    `deny` comes back empty and a MANDATORY gate reports `passed`.

    That is the failure the Sync button was fixed for. Fixing the button and
    not this path left the route `terrapod_policy_set` takes -- the one an
    operator managing policy as code actually uses -- on the fail-open side,
    reachable by a `terraform apply` with no commit behind it.
    """

    @pytest.mark.asyncio
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_switching_it_on_forces_a_resync(
        self, _init_db, _init_redis, _init_storage, mock_enqueue
    ):
        ps = _mock_policy_set(source="vcs")
        ps.shared_evaluation = False
        app, db = _make_app()
        db.commit = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}",
                json={"data": {"attributes": {"shared-evaluation": True}}},
                headers=_AUTH,
            )

        assert resp.status_code == 200
        mock_enqueue.assert_called_once()
        payload = mock_enqueue.call_args.kwargs["payload"]
        assert payload["force"] is True, (
            "an unforced sync is a no-op when the SHA has not moved, which is "
            "the whole case this exists for"
        )
        assert payload["policy_set_id"] == str(ps.id)

    @pytest.mark.asyncio
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_an_unrelated_edit_does_not_resync(
        self, _init_db, _init_redis, _init_storage, mock_enqueue
    ):
        """Narrow on purpose: a PATCH is not a sync request.

        Re-reading a repository on every description change would turn routine
        provider drift into VCS traffic on every policy set in the fleet.
        """
        ps = _mock_policy_set(source="vcs")
        ps.shared_evaluation = False
        app, db = _make_app()
        db.commit = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}",
                json={"data": {"attributes": {"description": "unrelated"}}},
                headers=_AUTH,
            )

        assert resp.status_code == 200
        mock_enqueue.assert_not_called()

    @pytest.mark.asyncio
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_leaving_it_on_does_not_resync(
        self, _init_db, _init_redis, _init_storage, mock_enqueue
    ):
        """A provider re-applying an unchanged `shared_evaluation = true` sends
        the attribute every time. Only the transition re-reads, or steady-state
        Terraform runs would sync the whole fleet on every plan."""
        ps = _mock_policy_set(source="vcs")
        ps.shared_evaluation = True
        app, db = _make_app()
        db.commit = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}",
                json={"data": {"attributes": {"shared-evaluation": True}}},
                headers=_AUTH,
            )

        assert resp.status_code == 200
        mock_enqueue.assert_not_called()

    @pytest.mark.asyncio
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_an_inline_set_has_no_repository_to_read(
        self, _init_db, _init_redis, _init_storage, mock_enqueue
    ):
        ps = _mock_policy_set(source="inline")
        ps.shared_evaluation = False
        app, db = _make_app()
        db.commit = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(ps))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/policy-sets/polset-{ps.id}",
                json={"data": {"attributes": {"shared-evaluation": True}}},
                headers=_AUTH,
            )

        assert resp.status_code == 200
        mock_enqueue.assert_not_called()
