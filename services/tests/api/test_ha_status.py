"""The replication status endpoint (#1121).

Before this, an operator could not answer "is my follower caught up?" — the one
question worth asking before moving DNS. These pin the answers it gives, and as
much the ones it refuses to give.

Everything is computed from local state. A status endpoint that calls the peer
stops working exactly when the peer is the problem, which is when it is read.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import get_current_user
from terrapod.api.routers.ha import router
from terrapod.db.session import get_db
from terrapod.services.replication import ReplicationStatus


def _app(roles: list[str] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/terrapod/v1")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        email="a@x.com", roles=roles if roles is not None else ["admin"]
    )
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


def _ha(role="leader", peer_url="https://peer", enabled=True, retention_days=7):
    return SimpleNamespace(
        role=role,
        node_name="node-a",
        peer=SimpleNamespace(url=peer_url),
        replication=SimpleNamespace(enabled=enabled, retention_days=retention_days),
    )


async def _get(state: ReplicationStatus, ha=None, roles: list[str] | None = None):
    cfg = ha or _ha()
    with (
        patch("terrapod.services.replication.read_status", new_callable=AsyncMock) as mock_state,
        patch("terrapod.api.routers.ha.settings") as mock_settings,
        patch("terrapod.services.ha_role.get_role", new_callable=AsyncMock) as mock_role,
    ):
        mock_state.return_value = state
        mock_settings.ha = cfg
        mock_role.return_value = cfg.role
        transport = ASGITransport(app=_app(roles))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/api/terrapod/v1/ha/status")


class TestFollowerConvergence:
    async def test_a_recent_sync_with_no_backfill_is_in_sync(self):
        resp = await _get(
            ReplicationStatus(last_sync_at=datetime.now(UTC) - timedelta(seconds=30)),
            ha=_ha(role="follower"),
        )

        attrs = resp.json()["data"]["attributes"]
        assert attrs["in-sync"] is True
        assert 25 <= attrs["seconds-since-last-sync"] <= 60

    async def test_backfilling_is_not_in_sync_however_recent_the_pull(self):
        """The failure this endpoint exists to prevent: reading only the
        timestamp would tell somebody about to move DNS that a node still
        pulling a whole class is ready."""
        resp = await _get(
            ReplicationStatus(last_sync_at=datetime.now(UTC), backfilling=["api_tokens", "users"]),
            ha=_ha(role="follower"),
        )

        attrs = resp.json()["data"]["attributes"]
        assert attrs["in-sync"] is False
        assert attrs["seconds-since-last-sync"] < 5
        assert attrs["backfilling-classes"] == ["api_tokens", "users"]

    async def test_a_node_that_has_never_synced_is_not_in_sync(self):
        resp = await _get(ReplicationStatus(), ha=_ha(role="follower"))

        attrs = resp.json()["data"]["attributes"]
        assert attrs["in-sync"] is False
        assert attrs["last-sync-at"] is None
        assert attrs["seconds-since-last-sync"] is None

    async def test_replication_off_is_never_in_sync(self):
        """A node not replicating cannot be caught up — saying so would be a
        green light on a node receiving nothing."""
        resp = await _get(ReplicationStatus(last_sync_at=datetime.now(UTC)), ha=_ha(enabled=False))

        assert resp.json()["data"]["attributes"]["in-sync"] is False


class TestLeaderMargin:
    async def test_reports_the_retention_window_and_oldest_event(self):
        """The leader's early warning: as these converge, the follower is close
        to falling off the end and having to backfill from scratch."""
        resp = await _get(
            ReplicationStatus(
                events_retained=1200, oldest_event_at=datetime.now(UTC) - timedelta(days=6)
            )
        )

        attrs = resp.json()["data"]["attributes"]
        assert attrs["events-retained"] == 1200
        assert attrs["retention-seconds"] == 7 * 86400
        assert attrs["oldest-event-age-seconds"] > 5 * 86400

    async def test_an_empty_outbox_reports_no_age(self):
        resp = await _get(ReplicationStatus())

        attrs = resp.json()["data"]["attributes"]
        assert attrs["events-retained"] == 0
        assert attrs["oldest-event-age-seconds"] is None


class TestSingleNode:
    async def test_reports_that_there_is_no_peer(self):
        """A single node must be legible as such, not as a pair failing to
        converge."""
        resp = await _get(ReplicationStatus(), ha=_ha(peer_url="", enabled=False))

        attrs = resp.json()["data"]["attributes"]
        assert attrs["peer-configured"] is False
        assert attrs["replication-enabled"] is False


class TestScope:
    async def test_lists_the_replicated_classes(self):
        """So an operator can see what a failover would and would not carry."""
        classes = (await _get(ReplicationStatus())).json()["data"]["attributes"][
            "replicated-classes"
        ]

        assert "api_tokens" in classes
        assert "agent_pools" in classes


class TestNoPeerCall:
    def test_status_is_answered_without_reaching_the_peer(self):
        """A status endpoint that calls the peer stops working exactly when the
        peer is the problem. Asserted structurally — the regression would be
        silent until an incident."""
        import inspect

        from terrapod.api.routers import ha

        source = inspect.getsource(ha)
        assert "httpx" not in source
        assert "arequest_with_retry" not in source


class TestWhoMaySeeWhat:
    """#1165 — the node's own disposition is not privileged; the cluster's is.

    The split exists because hiding "you are talking to a follower" from the
    person whose next apply is about to be refused is the opposite of useful,
    while ready-vs-desired per pod and node/zone concentration describe the
    deployment rather than the node and stay with admin/audit.
    """

    async def test_an_ordinary_user_can_read_the_nodes_own_role_and_sync_state(self):
        resp = await _get(
            ReplicationStatus(last_sync_at=datetime.now(UTC) - timedelta(seconds=5)),
            ha=_ha(role="follower"),
            roles=["everyone"],
        )

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["role"] == "follower"
        assert attrs["peer-configured"] is True
        assert attrs["in-sync"] is True

    async def test_an_ordinary_user_is_told_the_cluster_half_is_restricted(self):
        # Distinct from `components-unavailable-reason`: an admin debugging
        # "I cannot see the cluster" must never be shown "you may not".
        resp = await _get(ReplicationStatus(), roles=["everyone"])

        attrs = resp.json()["data"]["attributes"]
        assert attrs["components-restricted"] is True
        assert attrs["components"] == []
        assert attrs["ha-findings"] == []
        assert attrs["single-replica-components"] == []
        assert attrs["components-unavailable-reason"] is None

    async def test_an_unprivileged_read_does_not_touch_kubernetes(self):
        # Filtering the response would still have made every page load in every
        # session hit the Kubernetes API. The read must not happen at all.
        with patch("terrapod.services.component_status.read", new_callable=AsyncMock) as mock_read:
            await _get(ReplicationStatus(), roles=["everyone"])

        mock_read.assert_not_awaited()

    async def test_an_auditor_sees_the_cluster_half(self):
        resp = await _get(ReplicationStatus(), roles=["audit"])

        assert resp.json()["data"]["attributes"]["components-restricted"] is False

    async def test_an_admin_sees_the_cluster_half(self):
        resp = await _get(ReplicationStatus(), roles=["admin"])

        assert resp.json()["data"]["attributes"]["components-restricted"] is False
