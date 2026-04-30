"""Tests for agent pool endpoints including RBAC gating."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import (
    AuthenticatedUser,
    ListenerIdentity,
    get_current_user,
    get_listener_identity,
    require_admin,
)
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="test@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _mock_pool(pool_id=None, name="test-pool", labels=None, owner_email=None):
    pool = MagicMock()
    pool.id = pool_id or uuid.uuid4()
    pool.name = name
    pool.description = "A test pool"
    pool.labels = labels or {}
    pool.owner_email = owner_email
    pool.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    pool.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return pool


def _make_app(user, mock_db=None, is_admin=False):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if is_admin:
        app.dependency_overrides[require_admin] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _mock_listener_dict(listener_id=None, name="listener-1", pool_id=None):
    """Return a dict matching the Redis-backed listener shape."""
    return {
        "id": str(listener_id or uuid.uuid4()),
        "name": name,
        "pool_id": str(pool_id or uuid.uuid4()),
        "status": "online",
        "capacity": "10",
        "active_runs": "0",
        "last_heartbeat": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
    }


class TestListenerHeartbeat:
    """Heartbeat must be cert-authenticated.

    Without auth, an attacker (or a misconfigured listener) could keep a
    dead listener "online" by spamming heartbeats — masking a cert auth
    failure that would otherwise mark the listener offline. The cert's
    listener-id must also match the path id.
    """

    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.heartbeat_listener", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_sets_redis_keys(self, mock_get_listener, mock_heartbeat, mock_publish):
        lid = uuid.uuid4()
        listener = _mock_listener_dict(listener_id=lid)
        mock_get_listener.return_value = listener

        app = create_app()
        app.dependency_overrides[get_listener_identity] = lambda: _mock_listener_identity(lid)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{lid}/heartbeat",
                json={"capacity": 5, "active_runs": 2, "pod_name": "listener-abc-123"},
                headers={"X-Terrapod-Client-Cert": "irrelevant-mocked"},
            )

        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

        # Verify heartbeat_listener was called with correct args
        mock_heartbeat.assert_called_once()
        call_kwargs = mock_heartbeat.call_args.kwargs
        assert call_kwargs["listener_id"] == str(lid)
        assert call_kwargs["name"] == "listener-1"
        assert call_kwargs["pod_name"] == "listener-abc-123"
        assert call_kwargs["capacity"] == "5"
        assert call_kwargs["active_runs"] == "2"

    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.heartbeat_listener", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_without_pod_name_passes_none(
        self, mock_get_listener, mock_heartbeat, mock_publish
    ):
        """Older clients that don't send pod_name still work; pod_name comes through as None."""
        lid = uuid.uuid4()
        mock_get_listener.return_value = _mock_listener_dict(listener_id=lid)

        app = create_app()
        app.dependency_overrides[get_listener_identity] = lambda: _mock_listener_identity(lid)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{lid}/heartbeat",
                json={"capacity": 5, "active_runs": 0},
                headers={"X-Terrapod-Client-Cert": "irrelevant-mocked"},
            )

        assert res.status_code == 200
        mock_heartbeat.assert_called_once()
        assert mock_heartbeat.call_args.kwargs["pod_name"] is None

    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.heartbeat_listener", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_publishes_admin_event(
        self, mock_get_listener, mock_heartbeat, mock_publish
    ):
        lid = uuid.uuid4()
        listener = _mock_listener_dict(listener_id=lid)
        mock_get_listener.return_value = listener

        app = create_app()
        app.dependency_overrides[get_listener_identity] = lambda: _mock_listener_identity(lid)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{lid}/heartbeat",
                json={"capacity": 3, "active_runs": 1},
                headers={"X-Terrapod-Client-Cert": "irrelevant-mocked"},
            )

        assert res.status_code == 200

        # Verify publish_event was called with admin channel
        assert mock_publish.call_count >= 1
        channels = [call.args[0] for call in mock_publish.call_args_list]
        assert "tp:admin_events" in channels

    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_heartbeat_not_found(self, mock_get_listener):
        mock_get_listener.return_value = None

        lid = uuid.uuid4()
        app = create_app()
        app.dependency_overrides[get_listener_identity] = lambda: _mock_listener_identity(lid)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{lid}/heartbeat",
                json={"capacity": 1},
                headers={"X-Terrapod-Client-Cert": "irrelevant-mocked"},
            )

        assert res.status_code == 404

    async def test_heartbeat_without_cert_returns_401(self):
        """No client cert header → 401 from get_listener_identity."""
        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{uuid.uuid4()}/heartbeat",
                json={"capacity": 1},
            )

        assert res.status_code == 401

    async def test_heartbeat_cert_listener_id_mismatch_returns_403(self):
        """Cert authenticates as listener A but URL targets listener B → 403."""
        cert_lid = uuid.uuid4()
        path_lid = uuid.uuid4()

        app = create_app()
        app.dependency_overrides[get_listener_identity] = lambda: _mock_listener_identity(cert_lid)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{path_lid}/heartbeat",
                json={"capacity": 1},
                headers={"X-Terrapod-Client-Cert": "irrelevant-mocked"},
            )

        assert res.status_code == 403


class TestListPoolsRBAC:
    """Pool listing is RBAC-filtered: only pools the user has read access to."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_pools", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.api.routers.agent_pools.fetch_custom_roles",
        new_callable=AsyncMock,
    )
    async def test_list_pools_filters_by_rbac(
        self, mock_fetch_roles, mock_resolve, mock_list_pools, mock_list_listeners, *mocks
    ):
        """User only sees pools they have permission on."""
        pool_visible = _mock_pool(name="visible-pool", labels={"env": "dev"})
        pool_hidden = _mock_pool(name="hidden-pool", labels={"env": "prod"})
        mock_list_pools.return_value = [pool_visible, pool_hidden]
        mock_fetch_roles.return_value = []
        mock_list_listeners.return_value = []

        # First pool: read access; second pool: no access
        mock_resolve.side_effect = ["read", None]

        user = _user(roles=["everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get("/api/v2/organizations/default/agent-pools", headers=_AUTH)

        assert res.status_code == 200
        data = res.json()["data"]
        assert len(data) == 1
        assert data[0]["attributes"]["name"] == "visible-pool"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_pools", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.api.routers.agent_pools.fetch_custom_roles",
        new_callable=AsyncMock,
    )
    async def test_list_pools_returns_permission(
        self, mock_fetch_roles, mock_resolve, mock_list_pools, mock_list_listeners, *mocks
    ):
        """Response includes the user's effective permission on each pool."""
        pool = _mock_pool(name="my-pool")
        mock_list_pools.return_value = [pool]
        mock_fetch_roles.return_value = []
        mock_list_listeners.return_value = []
        mock_resolve.return_value = "write"

        user = _user(roles=["pool-writer"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get("/api/v2/organizations/default/agent-pools", headers=_AUTH)

        assert res.status_code == 200
        data = res.json()["data"]
        assert data[0]["attributes"]["permission"] == "write"


class TestPoolStatus:
    """Pool list/detail surfaces a status string derived from registered listeners."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_pools", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.api.routers.agent_pools.fetch_custom_roles",
        new_callable=AsyncMock,
    )
    async def test_list_pools_status_online_when_listener_online(
        self, mock_fetch_roles, mock_resolve, mock_list_pools, mock_list_listeners, *mocks
    ):
        pool = _mock_pool(name="busy-pool")
        mock_list_pools.return_value = [pool]
        mock_fetch_roles.return_value = []
        mock_resolve.return_value = "read"
        mock_list_listeners.return_value = [
            {"id": str(uuid.uuid4()), "name": "lis-1", "status": "online"}
        ]

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get("/api/v2/organizations/default/agent-pools", headers=_AUTH)

        assert res.status_code == 200
        attrs = res.json()["data"][0]["attributes"]
        assert attrs["status"] == "online"
        # listener-summary is gone — replaced by a single status pill
        assert "listener-summary" not in attrs

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_pools", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.api.routers.agent_pools.fetch_custom_roles",
        new_callable=AsyncMock,
    )
    async def test_list_pools_status_offline_when_no_listeners(
        self, mock_fetch_roles, mock_resolve, mock_list_pools, mock_list_listeners, *mocks
    ):
        pool = _mock_pool(name="quiet-pool")
        mock_list_pools.return_value = [pool]
        mock_fetch_roles.return_value = []
        mock_resolve.return_value = "read"
        mock_list_listeners.return_value = []

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get("/api/v2/organizations/default/agent-pools", headers=_AUTH)

        assert res.json()["data"][0]["attributes"]["status"] == "offline"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_show_pool_includes_status(
        self, mock_resolve, mock_get_pool, mock_list_listeners, *mocks
    ):
        pool = _mock_pool(name="visible")
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "read"
        mock_list_listeners.return_value = [
            {"id": str(uuid.uuid4()), "name": "lis", "status": "online"}
        ]

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/agent-pools/apool-{pool.id}", headers=_AUTH)

        assert res.json()["data"]["attributes"]["status"] == "online"


# ── Truthful pool/listener status (cert-expiry cross-check) ──────────


class TestDeriveListenerStatus:
    """Cross-check heartbeat-reported status against cert expiry.

    The heartbeat path blindly writes `status: "online"` regardless of cert
    validity. A listener whose cert has expired keeps heartbeating but every
    cert-authenticated call (claim run, renew, runner-token, etc.) 401s — it
    isn't actually working. The derived status must reflect that.
    """

    def test_online_when_cert_valid(self):
        from terrapod.api.routers.agent_pools import _derive_listener_status

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        listener = {"status": "online", "certificate_expires_at": future}
        assert _derive_listener_status(listener) == "online"

    def test_cert_expired_when_expiry_in_past(self):
        from terrapod.api.routers.agent_pools import _derive_listener_status

        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        listener = {"status": "online", "certificate_expires_at": past}
        assert _derive_listener_status(listener) == "cert-expired"

    def test_falls_back_to_raw_status_when_no_expiry_field(self):
        """Older listeners may not populate the field — preserve the raw value."""
        from terrapod.api.routers.agent_pools import _derive_listener_status

        assert _derive_listener_status({"status": "offline"}) == "offline"
        assert _derive_listener_status({"status": "online"}) == "online"

    def test_falls_back_to_raw_status_on_unparseable_expiry(self):
        from terrapod.api.routers.agent_pools import _derive_listener_status

        listener = {"status": "online", "certificate_expires_at": "garbage"}
        assert _derive_listener_status(listener) == "online"


class TestDerivePoolStatus:
    def test_offline_when_no_listeners(self):
        from terrapod.api.routers.agent_pools import _derive_pool_status

        assert _derive_pool_status([]) == "offline"

    def test_online_when_any_listener_healthy(self):
        from terrapod.api.routers.agent_pools import _derive_pool_status

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        listeners = [
            {"status": "online", "certificate_expires_at": past},  # cert-expired
            {"status": "online", "certificate_expires_at": future},  # online
        ]
        assert _derive_pool_status(listeners) == "online"

    def test_degraded_when_all_certs_expired(self):
        """All listeners heartbeating but every cert is past expiry → pool can't run anything."""
        from terrapod.api.routers.agent_pools import _derive_pool_status

        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        listeners = [
            {"status": "online", "certificate_expires_at": past},
            {"status": "online", "certificate_expires_at": past},
        ]
        assert _derive_pool_status(listeners) == "degraded"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_pools", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.api.routers.agent_pools.fetch_custom_roles",
        new_callable=AsyncMock,
    )
    async def test_list_pools_returns_degraded_when_all_certs_expired(
        self, mock_fetch_roles, mock_resolve, mock_list_pools, mock_list_listeners, *mocks
    ):
        """End-to-end: heartbeating listener with expired cert surfaces as `degraded` at /agent-pools."""
        pool = _mock_pool(name="dying-pool")
        mock_list_pools.return_value = [pool]
        mock_fetch_roles.return_value = []
        mock_resolve.return_value = "read"
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        mock_list_listeners.return_value = [
            {
                "id": str(uuid.uuid4()),
                "name": "lis",
                "status": "online",
                "certificate_expires_at": past,
            }
        ]

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get("/api/v2/organizations/default/agent-pools", headers=_AUTH)

        assert res.json()["data"][0]["attributes"]["status"] == "degraded"


class TestListListenersReplicaCount:
    """The /listeners endpoint enriches each listener with a replica count."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.count_listener_replicas", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_listeners_carry_replica_count(
        self,
        mock_resolve,
        mock_get_pool,
        mock_list_listeners,
        mock_count,
        *mocks,
    ):
        pool = _mock_pool()
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "read"
        lid_a, lid_b = uuid.uuid4(), uuid.uuid4()
        mock_list_listeners.return_value = [
            {"id": str(lid_a), "name": "lis-a", "status": "online", "tracks_pods": "1"},
            {"id": str(lid_b), "name": "lis-b", "status": "online", "tracks_pods": "1"},
        ]
        # 3 pods for lis-a, 1 pod for lis-b
        mock_count.side_effect = [3, 1]

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/agent-pools/apool-{pool.id}/listeners", headers=_AUTH)

        assert res.status_code == 200
        data = res.json()["data"]
        by_id = {item["id"]: item["attributes"] for item in data}
        assert by_id[f"listener-{lid_a}"]["replica-count"] == 3
        assert by_id[f"listener-{lid_b}"]["replica-count"] == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.count_listener_replicas", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_replica_count_omitted_when_listener_does_not_track_pods(
        self,
        mock_resolve,
        mock_get_pool,
        mock_list_listeners,
        mock_count,
        *mocks,
    ):
        """Pre-0.19.0 listeners (no tracks_pods flag) → no replica-count attr,
        and count_listener_replicas is never called."""
        pool = _mock_pool()
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "read"
        lid = uuid.uuid4()
        mock_list_listeners.return_value = [
            {"id": str(lid), "name": "old-listener", "status": "online"},
        ]

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/agent-pools/apool-{pool.id}/listeners", headers=_AUTH)

        assert res.status_code == 200
        attrs = res.json()["data"][0]["attributes"]
        assert "replica-count" not in attrs
        mock_count.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.count_listener_replicas", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_replica_count_included_for_mixed_listeners(
        self,
        mock_resolve,
        mock_get_pool,
        mock_list_listeners,
        mock_count,
        *mocks,
    ):
        """Mixed list: tracking listener gets replica-count, old one doesn't."""
        pool = _mock_pool()
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "read"
        lid_new, lid_old = uuid.uuid4(), uuid.uuid4()
        mock_list_listeners.return_value = [
            {"id": str(lid_new), "name": "new", "status": "online", "tracks_pods": "1"},
            {"id": str(lid_old), "name": "old", "status": "online"},
        ]
        mock_count.side_effect = [2]  # only one count call expected

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/agent-pools/apool-{pool.id}/listeners", headers=_AUTH)

        data = {item["id"]: item["attributes"] for item in res.json()["data"]}
        assert data[f"listener-{lid_new}"]["replica-count"] == 2
        assert "replica-count" not in data[f"listener-{lid_old}"]
        assert mock_count.call_count == 1


class TestShowPoolRBAC:
    """Show pool requires read permission."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.list_listeners", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_show_pool_with_read(
        self, mock_resolve, mock_get_pool, mock_list_listeners, *mocks
    ):
        pool = _mock_pool(name="visible", labels={"env": "dev"}, owner_email="owner@test.com")
        mock_get_pool.return_value = pool
        mock_list_listeners.return_value = []
        mock_resolve.return_value = "read"

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/agent-pools/apool-{pool.id}", headers=_AUTH)

        assert res.status_code == 200
        attrs = res.json()["data"]["attributes"]
        assert attrs["name"] == "visible"
        assert attrs["labels"] == {"env": "dev"}
        assert attrs["owner-email"] == "owner@test.com"
        assert attrs["permission"] == "read"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_show_pool_no_access_returns_404(self, mock_resolve, mock_get_pool, *mocks):
        """Pool invisible to user returns 404 (not 403)."""
        pool = _mock_pool(name="secret")
        mock_get_pool.return_value = pool
        mock_resolve.return_value = None

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/agent-pools/apool-{pool.id}", headers=_AUTH)

        assert res.status_code == 404


class TestCreatePoolRBAC:
    """Pool creation is admin-only and accepts labels/owner."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.create_pool", new_callable=AsyncMock)
    async def test_create_pool_with_labels_and_owner(self, mock_create, *mocks):
        pool = _mock_pool(
            name="new-pool",
            labels={"env": "prod", "team": "sre"},
            owner_email="sre@example.com",
        )
        mock_create.return_value = pool

        user = _user(email="admin@example.com", roles=["admin"])
        app, mock_db = _make_app(user, is_admin=True)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                "/api/v2/organizations/default/agent-pools",
                json={
                    "data": {
                        "type": "agent-pools",
                        "attributes": {
                            "name": "new-pool",
                            "labels": {"env": "prod", "team": "sre"},
                            "owner-email": "sre@example.com",
                        },
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 201
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["labels"] == {"env": "prod", "team": "sre"}
        assert call_kwargs["owner_email"] == "sre@example.com"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.create_pool", new_callable=AsyncMock)
    async def test_create_pool_invalid_owner_email_422(self, mock_create, *mocks):
        user = _user(email="admin@example.com", roles=["admin"])
        app, mock_db = _make_app(user, is_admin=True)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                "/api/v2/organizations/default/agent-pools",
                json={
                    "data": {
                        "type": "agent-pools",
                        "attributes": {
                            "name": "bad-pool",
                            "owner-email": "not-an-email",
                        },
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 422
        assert "owner-email" in res.json()["detail"]


class TestUpdatePoolRBAC:
    """Pool update requires admin permission on the pool."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.update_pool", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_update_pool_with_labels(self, mock_resolve, mock_get_pool, mock_update, *mocks):
        pool = _mock_pool(name="my-pool", labels={"env": "dev"})
        updated_pool = _mock_pool(name="my-pool", labels={"env": "prod"})
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "admin"
        mock_update.return_value = updated_pool

        user = _user(email="owner@example.com")
        app, mock_db = _make_app(user)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/agent-pools/apool-{pool.id}",
                json={
                    "data": {
                        "attributes": {
                            "labels": {"env": "prod"},
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_update_pool_write_only_returns_403(self, mock_resolve, mock_get_pool, *mocks):
        """Write permission is insufficient for pool update — admin required."""
        pool = _mock_pool(name="restricted")
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "write"

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/agent-pools/apool-{pool.id}",
                json={"data": {"attributes": {"description": "updated"}}},
                headers=_AUTH,
            )

        assert res.status_code == 403


class TestDeletePoolRBAC:
    """Pool delete requires admin permission on the pool."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.delete_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.services.agent_pool_service.delete_pool_listeners",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_delete_pool_with_admin(
        self, mock_resolve, mock_get_pool, mock_del_listeners, mock_del_pool, *mocks
    ):
        pool = _mock_pool()
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "admin"

        user = _user(email="owner@example.com")
        app, mock_db = _make_app(user)
        mock_db.commit = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.delete(f"/api/v2/agent-pools/apool-{pool.id}", headers=_AUTH)

        assert res.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_delete_pool_no_access_returns_404(self, mock_resolve, mock_get_pool, *mocks):
        pool = _mock_pool()
        mock_get_pool.return_value = pool
        mock_resolve.return_value = None

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.delete(f"/api/v2/agent-pools/apool-{pool.id}", headers=_AUTH)

        assert res.status_code == 404


class TestTokenEndpointRBAC:
    """Token endpoints require admin permission — read-only gets 403."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_list_tokens_read_only_returns_403(self, mock_resolve, mock_get_pool, *mocks):
        """User with read permission cannot list pool tokens."""
        pool = _mock_pool()
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "read"

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.get(f"/api/v2/agent-pools/apool-{pool.id}/tokens", headers=_AUTH)

        assert res.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_create_token_read_only_returns_403(self, mock_resolve, mock_get_pool, *mocks):
        """User with read permission cannot create pool tokens."""
        pool = _mock_pool()
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "read"

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/agent-pools/apool-{pool.id}/tokens",
                json={"data": {"attributes": {"description": "test"}}},
                headers=_AUTH,
            )

        assert res.status_code == 403


class TestUpdatePoolSelfLockout:
    """Self-lockout protection prevents accidental access loss."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_label_change_reducing_access_returns_409(
        self, mock_resolve, mock_get_pool, *mocks
    ):
        """Changing labels that would reduce user's access returns 409."""
        pool = _mock_pool(name="my-pool", labels={"team": "sre"})
        mock_get_pool.return_value = pool
        # First call: current permission check → admin
        # Second call: simulated new permission → None (locked out)
        mock_resolve.side_effect = ["admin", None]

        user = _user(email="user@example.com", roles=["sre-role"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/agent-pools/apool-{pool.id}",
                json={"data": {"attributes": {"labels": {"team": "other"}}}},
                headers=_AUTH,
            )

        assert res.status_code == 409
        assert "reduce your access" in res.json()["errors"][0]["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.update_pool", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_force_bypasses_lockout_check(
        self, mock_resolve, mock_get_pool, mock_update, *mocks
    ):
        """Setting force: true bypasses the self-lockout check."""
        pool = _mock_pool(name="my-pool", labels={"team": "sre"})
        updated_pool = _mock_pool(name="my-pool", labels={"team": "other"})
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "admin"
        mock_update.return_value = updated_pool

        user = _user(email="user@example.com", roles=["sre-role"])
        app, mock_db = _make_app(user)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/agent-pools/apool-{pool.id}",
                json={"data": {"attributes": {"labels": {"team": "other"}, "force": True}}},
                headers=_AUTH,
            )

        assert res.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.update_pool", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_platform_admin_immune_to_lockout(
        self, mock_resolve, mock_get_pool, mock_update, *mocks
    ):
        """Platform admins skip the self-lockout check entirely."""
        pool = _mock_pool(name="my-pool", labels={"team": "sre"})
        updated_pool = _mock_pool(name="my-pool", labels={})
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "admin"
        mock_update.return_value = updated_pool

        user = _user(email="admin@example.com", roles=["admin"])
        app, mock_db = _make_app(user)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/agent-pools/apool-{pool.id}",
                json={"data": {"attributes": {"labels": {}}}},
                headers=_AUTH,
            )

        assert res.status_code == 200


class TestDeleteListenerRBAC:
    """Listener delete requires admin on pool, or platform admin when pool can't be resolved."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.services.agent_pool_service.delete_listener",
        new_callable=AsyncMock,
    )
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_listener")
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_delete_listener_with_pool_admin(
        self, mock_resolve, mock_get_listener, mock_get_pool, mock_del, *mocks
    ):
        pool = _mock_pool()
        lid = uuid.uuid4()
        listener = _mock_listener_dict(listener_id=lid, pool_id=pool.id)
        mock_get_listener.return_value = listener
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "admin"

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.delete(f"/api/v2/listeners/{lid}", headers=_AUTH)

        assert res.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch("terrapod.services.agent_pool_service.get_listener")
    @patch(
        "terrapod.api.routers.agent_pools.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    async def test_delete_listener_read_only_returns_403(
        self, mock_resolve, mock_get_listener, mock_get_pool, *mocks
    ):
        """User with read permission cannot delete listeners."""
        pool = _mock_pool()
        lid = uuid.uuid4()
        listener = _mock_listener_dict(listener_id=lid, pool_id=pool.id)
        mock_get_listener.return_value = listener
        mock_get_pool.return_value = pool
        mock_resolve.return_value = "read"

        user = _user()
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.delete(f"/api/v2/listeners/{lid}", headers=_AUTH)

        assert res.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_delete_listener_no_pool_non_admin_403(self, mock_get_listener, *mocks):
        """When pool can't be resolved, non-admin gets 403."""
        lid = uuid.uuid4()
        listener = _mock_listener_dict(listener_id=lid)
        listener["pool_id"] = ""
        mock_get_listener.return_value = listener

        user = _user(roles=["everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.delete(f"/api/v2/listeners/{lid}", headers=_AUTH)

        assert res.status_code == 403


# ── Renew listener cert (cert-authenticated) ────────────────────────


def _mock_listener_identity(listener_id, name="listener-1", pool_id=None):
    return ListenerIdentity(
        listener_id=listener_id,
        name=name,
        pool_id=pool_id or uuid.uuid4(),
        certificate_fingerprint="dead" * 16,
        certificate_expires_at=None,
    )


class TestRenewListenerCert:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.renew_listener_certificate")
    @patch("terrapod.services.agent_pool_service.get_pool")
    @patch("terrapod.services.agent_pool_service.get_listener")
    async def test_succeeds_when_cert_matches_path(
        self,
        mock_get_listener,
        mock_get_pool,
        mock_renew,
        *mocks,
    ):
        """Cert listener_id matches the path → 200 + renewed cert returned."""
        lid = uuid.uuid4()
        pid = uuid.uuid4()
        mock_get_listener.return_value = _mock_listener_dict(
            listener_id=lid, name="listener-1", pool_id=pid
        )
        mock_get_pool.return_value = _mock_pool(pool_id=pid)
        mock_renew.return_value = {
            "certificate": "PEM",
            "private_key": "PEM",
            "ca_certificate": "PEM",
            "certificate_fingerprint": "abcd",
            "certificate_expires_at": "2026-01-01T01:00:00+00:00",
        }

        app = create_app()
        app.dependency_overrides[get_listener_identity] = lambda: _mock_listener_identity(
            lid, pool_id=pid
        )
        app.dependency_overrides[get_db] = lambda: AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{lid}/renew",
                headers={"X-Terrapod-Client-Cert": "irrelevant-mocked"},
            )

        assert res.status_code == 200
        assert res.json()["data"]["certificate"] == "PEM"
        assert mock_renew.await_count == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.agent_pool_service.renew_listener_certificate")
    async def test_rejects_path_listener_id_mismatch(self, mock_renew, *mocks):
        """Cert authenticates as listener-A; trying to renew listener-B → 403."""
        cert_lid = uuid.uuid4()
        path_lid = uuid.uuid4()  # different from cert_lid

        app = create_app()
        app.dependency_overrides[get_listener_identity] = lambda: _mock_listener_identity(cert_lid)
        app.dependency_overrides[get_db] = lambda: AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(
                f"/api/v2/listeners/{path_lid}/renew",
                headers={"X-Terrapod-Client-Cert": "irrelevant-mocked"},
            )

        assert res.status_code == 403
        assert "Certificate does not match" in res.json()["detail"]
        # Renew was never called — auth check rejects before service layer
        assert mock_renew.await_count == 0

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_rejects_request_without_cert_header(self, *mocks):
        """No X-Terrapod-Client-Cert → 401 from the dep itself (no override)."""
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.post(f"/api/v2/listeners/{uuid.uuid4()}/renew")

        assert res.status_code == 401
        assert "X-Terrapod-Client-Cert" in res.json()["detail"]
