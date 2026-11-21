"""The jobs that walk storage find each workspace's objects in its own location."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.cli import restore_verify as rv
from terrapod.config import S3Config, StorageBackend, StorageLocationConfig
from terrapod.config import settings as app_settings
from terrapod.services import deleted_workspace_service as dws
from terrapod.services.artifact_retention_service import (
    RUN_ARTIFACT_KEYS,
    _cleanup_config_versions,
    _cleanup_config_versions_count,
    _cleanup_deleted_workspaces,
    _cleanup_run_artifacts,
    _cleanup_state_versions,
)
from terrapod.storage import keys
from terrapod.storage.routing import FanOutResolver, RoutingStore
from tests.services.test_artifact_retention_service import (
    _FakeResult,
    _FakeTupleResult,
    _make_cv,
    _make_run,
    _make_state_version,
)
from tests.services.test_deleted_workspace_markers import WS_A, WS_B, _db, _marker
from tests.storage.test_routing import FakeStore

EU = "eu-west"


class Resolver:
    """Live workspaces by location; anything else is unknown, as a deleted row is."""

    def __init__(self, live: dict[str, str] | None = None) -> None:
        self.live = live or {}

    async def location_for(self, workspace_id: str) -> str | None:
        return self.live.get(workspace_id)


class _NewOnEu:
    """The deleted workspace is unknown; the restored one lives on the named location."""

    async def location_for(self, workspace_id: str) -> str | None:
        return None if workspace_id == WS_A else EU


@pytest.fixture
def stores() -> tuple[FakeStore, FakeStore]:
    return FakeStore("home", 3600), FakeStore("eu", 900)


def _router(stores, resolver) -> RoutingStore:
    home, eu = stores
    return RoutingStore(home, {EU: eu}, resolver)


class TestTheOrphanScanCoversEveryLocation:
    async def test_an_orphan_in_a_named_location_is_stamped_on_default(self, stores):
        home, eu = stores
        eu.objects[keys.state_key(WS_A, "sv-1")] = b"{}"
        router = _router(stores, Resolver())

        assert await _cleanup_deleted_workspaces(_db(live_ids=[]), router, 30, 100) == 0

        marker = json.loads(home.objects[keys.deleted_workspace_marker_key(WS_A)])
        assert marker["marker_reason"] == dws.REASON_DISCOVERED
        assert keys.state_key(WS_A, "sv-1") in eu.objects
        assert not any(k.startswith(keys.DELETED_MARKER_PREFIX) for k in eu.objects)

    async def test_an_expired_orphan_is_reaped_from_its_location(self, stores):
        home, eu = stores
        for sv in ("sv-1", "sv-2"):
            eu.objects[keys.state_key(WS_A, sv)] = b"{}"
        home.objects[keys.state_key(WS_B, "sv-1")] = b"{}"
        for ws in (WS_A, WS_B):
            home.objects[keys.deleted_workspace_marker_key(ws)] = _marker(ws, age_days=31)
        router = _router(stores, Resolver())

        assert await _cleanup_deleted_workspaces(_db(live_ids=[]), router, 30, 100) == 2

        assert not eu.objects and not home.objects

    async def test_a_live_workspace_in_a_named_location_is_never_touched(self, stores):
        home, eu = stores
        key = keys.state_key(WS_B, "sv-1")
        eu.objects[key] = b"{}"
        router = _router(stores, Resolver({WS_B: EU}))

        assert await _cleanup_deleted_workspaces(_db(live_ids=[WS_B]), router, 30, 100) == 0

        assert key in eu.objects
        assert keys.deleted_workspace_marker_key(WS_B) not in home.objects
        assert not [c for c in eu.calls if c[0] == "delete"]


class TestTheReaperResolvesOncePerWorkspace:
    """The workspace row is gone -- that is what makes it reapable -- so every
    per-object delete used to miss the resolver, query the database (negative
    lookups are deliberately never cached) and then fan the delete across every
    configured location. The marker the reaper has already read knows where the
    objects are (#2621)."""

    class _Counting:
        """Counts UNCACHED resolutions, which is what costs a query.

        Counting calls would not distinguish the fix: the real resolver is still
        asked once per object, it just answers them from its cache.
        """

        def __init__(self) -> None:
            self.known: dict[str, str] = {}
            self.misses = 0

        async def location_for(self, workspace_id: str) -> str | None:
            if workspace_id in self.known:
                return self.known[workspace_id]
            self.misses += 1
            return None

        def remember(self, workspace_id: str, location: str | None) -> None:
            self.known[workspace_id] = location or "default"

    async def test_a_whole_workspace_costs_no_uncached_resolution(self, stores):
        home, eu = stores
        for sv in ("sv-1", "sv-2", "sv-3", "sv-4"):
            eu.objects[keys.state_key(WS_A, sv)] = b"{}"
        body = json.loads(_marker(WS_A, age_days=31))
        body["storage_location"] = EU
        home.objects[keys.deleted_workspace_marker_key(WS_A)] = json.dumps(body).encode()

        resolver = self._Counting()
        router = _router(stores, resolver)

        assert await _cleanup_deleted_workspaces(_db(live_ids=[]), router, 30, 100) == 1
        assert not eu.objects and not home.objects
        assert resolver.misses == 0, f"{resolver.misses} uncached resolutions"


class TestRowDrivenDeletesGoToTheOwningLocation:
    async def test_excess_state_versions(self, stores):
        home, eu = stores
        ws = uuid.UUID(WS_A)
        svs = [_make_state_version(ws, serial=i) for i in range(2)]
        for sv in svs:
            eu.objects[keys.state_key(WS_A, str(sv.id))] = b"{}"
        db = AsyncMock()
        db.execute.side_effect = [_FakeTupleResult([(ws, False)]), _FakeResult(svs)]

        await _cleanup_state_versions(db, _router(stores, Resolver({WS_A: EU})), 5, 100)

        assert not eu.objects and not home.calls

    async def test_run_artifacts(self, stores):
        home, eu = stores
        run = _make_run(uuid.UUID(WS_A))
        for key_fn in RUN_ARTIFACT_KEYS:
            eu.objects[key_fn(WS_A, str(run.id))] = b"x"
        db = AsyncMock()
        db.execute.side_effect = [_FakeResult([run]), _FakeResult(["web1"])]

        await _cleanup_run_artifacts(db, _router(stores, Resolver({WS_A: EU})), 90, 100)

        assert not eu.objects and not home.calls
        assert ("delete", keys.host_events_host_key(WS_A, str(run.id), "web1")) in eu.calls

    @pytest.mark.parametrize("count_based", [False, True])
    async def test_config_versions(self, stores, count_based):
        home, eu = stores
        ws = uuid.UUID(WS_A)
        cvs = [_make_cv(ws), _make_cv(ws)]
        for cv in cvs:
            eu.objects[keys.config_version_key(WS_A, str(cv.id))] = b"x"
        db = AsyncMock()
        router = _router(stores, Resolver({WS_A: EU}))
        if count_based:
            db.execute.side_effect = [_FakeTupleResult([(ws,)]), _FakeResult(cvs)]
            await _cleanup_config_versions_count(db, router, 1, 100)
        else:
            db.execute.return_value = _FakeResult(cvs)
            await _cleanup_config_versions(db, router, 90, 100)

        assert not eu.objects and not home.calls


def _state(serial: int) -> bytes:
    return json.dumps({"version": 4, "serial": serial, "lineage": "lin-1"}).encode()


class TestRestoreFindsStateInItsLocation:
    @pytest.fixture(autouse=True)
    def _locations(self, monkeypatch):
        eu = StorageLocationConfig(backend=StorageBackend.S3, s3=S3Config(bucket="eu-bucket"))
        monkeypatch.setattr(app_settings.storage, "locations", {EU: eu})

    @pytest.fixture
    def seeded(self, stores):
        home, eu = stores
        svs = [str(uuid.uuid4()) for _ in range(2)]
        for i, sv in enumerate(sorted(svs), start=1):
            eu.objects[keys.state_key(WS_A, sv)] = _state(i)
        body = dws.build_discovery_marker(WS_A)
        body["workspace_name"] = "app-prod"
        body["settings"] = {"engine": "terraform", "storage_location": EU}
        home.objects[keys.deleted_workspace_marker_key(WS_A)] = json.dumps(body).encode()
        return stores

    async def test_a_restore_reads_and_writes_the_workspaces_location(self, seeded):
        home, eu = seeded
        router = _router(seeded, _NewOnEu())
        db = _db()
        db.add = MagicMock(side_effect=lambda row: row.id or setattr(row, "id", uuid.uuid4()))

        with patch("terrapod.services.state_index_service.record_latest_state", AsyncMock()):
            ws, report = await dws.restore_workspace(db, router, WS_A, restored_by="a@example.com")

        assert report["state_versions_restored"] == 2
        assert ws.storage_location == EU
        restored = [k for k in eu.objects if k.startswith(f"state/{ws.id}/")]
        assert len(restored) == 2
        assert not any(k.startswith(f"state/{ws.id}/") for k in home.objects)

    async def test_the_deleted_list_counts_versions_in_the_location(self, seeded):
        router = _router(seeded, _NewOnEu())
        [marker] = await dws.attach_state_counts(router, [{"workspace_id": WS_A}])
        assert marker["state_versions_available"] == 2


class TestRestoreVerify:
    async def test_the_drill_finds_state_in_a_named_location(self, stores, monkeypatch):
        _, eu = stores
        eu.objects[keys.state_key(WS_A, "sv-1")] = b"{}"
        monkeypatch.setattr(rv, "get_storage", lambda: _router(stores, FanOutResolver()))
        assert await rv._verify_state_objects() == ([EU], [])

    async def test_the_drill_installs_a_resolver_that_needs_no_database(self, monkeypatch):
        class Stop(Exception):
            pass

        init = AsyncMock(side_effect=Stop)
        monkeypatch.setattr(rv, "init_storage", init)
        monkeypatch.setenv("TP_RESTORE_TARGET_URL", "postgresql://u@scratch:5432/db")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(Stop):
            await rv.restore_verify()
        assert isinstance(init.await_args.kwargs["resolver"], FanOutResolver)
