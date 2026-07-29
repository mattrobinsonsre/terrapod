"""The peer replication endpoints (#960 phase 3, #1110).

Two behaviours here are load-bearing rather than cosmetic:

- **404 on a missing entity is a normal answer**, not an error. The row was
  deleted between the event and the read; the follower skips it and the later
  delete event settles it. Treating it as a failure would wedge the stream.
- **`meta.stale-cursor`** is how a lagging follower learns it must backfill. An
  innocent-looking empty page in its place is the failure mode this whole design
  exists to avoid.
"""

import uuid
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import PeerIdentity, get_peer_identity
from terrapod.api.routers.replication import router
from terrapod.db.session import get_db
from terrapod.services import replication


def _app(db=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/terrapod/v1")
    app.dependency_overrides[get_peer_identity] = lambda: PeerIdentity(
        client_id="peer-b", token_id="tok-1"
    )
    app.dependency_overrides[get_db] = lambda: db or AsyncMock()
    return app


async def _get(path: str, db=None):
    transport = ASGITransport(app=_app(db))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


BASE = "/api/terrapod/v1/ha/replication"


class TestClasses:
    async def test_lists_the_scope_in_dependency_order(self):
        resp = await _get(f"{BASE}/classes")

        ids = [d["id"] for d in resp.json()["data"]]
        assert ids.index("agent_pools") < ids.index("agent_pool_tokens")

    async def test_advertises_the_merge_rules(self):
        """The follower reads these from the sender rather than assuming, so a
        skewed pair agrees on how a counter merges."""
        resp = await _get(f"{BASE}/classes")

        tokens = next(d for d in resp.json()["data"] if d["id"] == "agent_pool_tokens")
        assert "use_count" in tokens["attributes"]["monotonic-fields"]
        assert "is_revoked" in tokens["attributes"]["one-way-true-fields"]


class TestEvents:
    @patch("terrapod.services.replication.read_events", new_callable=AsyncMock)
    async def test_returns_events_and_a_cursor(self, mock_read):
        mock_read.return_value = replication.EventPage(
            events=[{"id": 5, "entity-class": "agent_pools"}], cursor=5, stale_cursor=False
        )

        resp = await _get(f"{BASE}/events?after=4")

        assert resp.status_code == 200
        assert resp.json()["meta"]["cursor"] == 5
        assert resp.json()["meta"]["stale-cursor"] is False

    @patch("terrapod.services.replication.read_events", new_callable=AsyncMock)
    async def test_surfaces_a_stale_cursor(self, mock_read):
        mock_read.return_value = replication.EventPage(events=[], cursor=99, stale_cursor=True)

        resp = await _get(f"{BASE}/events?after=1")

        assert resp.json()["meta"]["stale-cursor"] is True

    async def test_rejects_a_negative_cursor(self):
        resp = await _get(f"{BASE}/events?after=-1")

        assert resp.status_code == 422


class TestEntity:
    @patch("terrapod.services.replication.read_entity", new_callable=AsyncMock)
    async def test_returns_current_state(self, mock_read):
        entity_id = str(uuid.uuid4())
        mock_read.return_value = {"id": entity_id, "name": "aws-prod"}

        resp = await _get(f"{BASE}/entities/agent_pools/{entity_id}")

        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["name"] == "aws-prod"

    @patch("terrapod.services.replication.read_entity", new_callable=AsyncMock)
    async def test_404_when_the_row_is_gone(self, mock_read):
        """Expected, not exceptional: deleted after the event was recorded."""
        mock_read.return_value = None

        resp = await _get(f"{BASE}/entities/agent_pools/{uuid.uuid4()}")

        assert resp.status_code == 404

    async def test_404_on_an_unknown_class(self):
        resp = await _get(f"{BASE}/entities/not_a_class/{uuid.uuid4()}")

        assert resp.status_code == 404


class TestBackfill:
    @patch("terrapod.services.replication.read_backfill", new_callable=AsyncMock)
    async def test_pages_and_reports_completion(self, mock_read):
        rows = [{"id": str(uuid.uuid4()), "name": "a"}]
        mock_read.return_value = rows

        resp = await _get(f"{BASE}/backfill/agent_pools?limit=200")

        body = resp.json()
        assert body["meta"]["complete"] is True, "a short page means the class is exhausted"
        assert body["meta"]["cursor"] == rows[0]["id"]

    @patch("terrapod.services.replication.read_backfill", new_callable=AsyncMock)
    async def test_a_full_page_is_not_complete(self, mock_read):
        mock_read.return_value = [{"id": str(uuid.uuid4()), "name": f"p{i}"} for i in range(2)]

        resp = await _get(f"{BASE}/backfill/agent_pools?limit=2")

        assert resp.json()["meta"]["complete"] is False

    @patch("terrapod.services.replication.read_backfill", new_callable=AsyncMock)
    async def test_an_empty_class_is_complete_and_holds_the_cursor(self, mock_read):
        mock_read.return_value = []

        resp = await _get(f"{BASE}/backfill/agent_pools?after=abc")

        assert resp.json()["meta"]["complete"] is True
        assert resp.json()["meta"]["cursor"] == "abc"

    async def test_404_on_an_unknown_class(self):
        resp = await _get(f"{BASE}/backfill/not_a_class")

        assert resp.status_code == 404


class TestEveryRouteIsPeerGated:
    """A route added here without the dependency would expose entity contents
    to any authenticated user — so the gate is asserted structurally."""

    def test_all_routes_depend_on_peer_identity(self):
        for route in router.routes:
            names = [d.call.__name__ for d in route.dependant.dependencies if d.call]
            assert "get_peer_identity" in names, f"{route.path} is not peer-gated"
