"""Each workspace's objects land only in its own storage location (real Postgres and S3)."""

import hashlib
import io
import json
import os
import tarfile
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from terrapod.config import S3Config, StorageBackend, StorageLocationConfig, settings
from terrapod.db.session import get_db_session
from terrapod.services import deleted_workspace_service as dws
from terrapod.services.artifact_retention_service import _cleanup_deleted_workspaces
from terrapod.storage import get_storage, keys
from terrapod.storage.location_resolver import DbLocationResolver
from terrapod.storage.routing import RoutingStore
from tests.integration.conftest import AUTH, _test_lifespan, admin_user, set_auth

pytestmark = pytest.mark.integration

NATIVE = "/api/v1/workspaces"
LOCATIONS = ("eu-west", "us-east")


def _s3(prefix: str) -> S3Config:
    return S3Config(
        bucket=os.environ.get("S3_TEST_BUCKET", "terrapod-test"),
        region="us-east-1",
        endpoint_url=os.environ["LOCALSTACK_ENDPOINT"],
        prefix=prefix,
    )


@pytest.fixture
async def buckets(monkeypatch, tmp_path):
    """Two S3 locations on LocalStack, and a raw store per location to inspect them."""
    endpoint = os.environ.get("LOCALSTACK_ENDPOINT", "")
    if not endpoint:
        pytest.skip("LocalStack not available (set LOCALSTACK_ENDPOINT)")
    try:
        with urllib.request.urlopen(f"{endpoint}/_localstack/health", timeout=2):
            pass
    except Exception:
        pytest.skip("LocalStack not reachable")

    from terrapod.storage.s3 import S3Store

    run = uuid.uuid4().hex[:8]
    configs = {name: _s3(f"locations-{run}/{name}") for name in LOCATIONS}
    raw = {
        name: S3Store(
            bucket=c.bucket, region=c.region, prefix=c.prefix, endpoint_url=c.endpoint_url
        )
        for name, c in configs.items()
    }
    client = await raw["eu-west"]._get_client()
    try:
        await client.create_bucket(Bucket=configs["eu-west"].bucket)
    except Exception:  # noqa: BLE001 — the bucket may already exist
        pass
    monkeypatch.setattr(settings.storage.filesystem, "root_dir", str(tmp_path))
    monkeypatch.setattr(
        settings.storage,
        "locations",
        {
            name: StorageLocationConfig(backend=StorageBackend.S3, s3=c)
            for name, c in configs.items()
        },
    )
    yield raw
    for store in raw.values():
        await store.close()


@pytest.fixture
async def app(_create_tables, buckets):
    """The conftest app, started after the locations are configured."""
    from terrapod.api.app import create_application

    application = create_application()
    application.router.lifespan_context = _test_lifespan
    async with application.router.lifespan_context(application):
        store = get_storage()
        assert isinstance(store, RoutingStore) and isinstance(store.resolver, DbLocationResolver)
        yield application
    application.dependency_overrides.clear()


async def _keys(store) -> set[str]:
    return {m.key for m in await store.list_prefix("")}


async def _workspace(client, name: str, location: str | None) -> str:
    attrs = {"name": name} | ({"storage-location": location} if location else {})
    r = await client.post(NATIVE, json={"data": {"attributes": attrs}}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"].removeprefix("ws-")


async def _upload_state(client, ws_id: str, serial: int) -> None:
    body = json.dumps({"version": 4, "serial": serial, "lineage": f"lin-{ws_id}"}).encode()
    sv = await client.post(
        f"/api/v2/workspaces/ws-{ws_id}/state-versions",
        json={
            "data": {
                "type": "state-versions",
                "attributes": {
                    "serial": serial,
                    "lineage": f"lin-{ws_id}",
                    "md5": hashlib.md5(body).hexdigest(),  # noqa: S324 — TFE protocol field
                },
            }
        },
        headers=AUTH,
    )
    assert sv.status_code == 201, sv.text
    up = await client.put(sv.json()["data"]["attributes"]["hosted-state-upload-url"], content=body)
    assert up.status_code == 200, up.text


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'output "x" { value = 1 }\n'
        info = tarfile.TarInfo("main.tf")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _upload_config(client, ws_id: str) -> None:
    cv = await client.post(
        f"/api/v2/workspaces/ws-{ws_id}/configuration-versions",
        json={"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}},
        headers=AUTH,
    )
    assert cv.status_code == 201, cv.text
    up = await client.put(
        cv.json()["data"]["attributes"]["upload-url"],
        content=_tarball(),
        headers={"Content-Type": "application/x-tar"},
    )
    assert up.status_code in (200, 204), up.text


async def _write_run_objects(ws_id: str) -> None:
    run_id = str(uuid.uuid4())
    store = get_storage()
    await store.put(keys.plan_log_key(ws_id, run_id), b"plan log")
    await store.put(keys.apply_log_key(ws_id, run_id), b"apply log")
    await store.put(keys.plan_output_key(ws_id, run_id), b"tfplan")


def _owned(found: set[str], ws_id: str) -> set[str]:
    return {k for k in found if keys.workspace_id_of_key(k) == ws_id}


class TestObjectsLandInTheirOwnLocation:
    async def test_state_config_and_run_objects_stay_in_the_owning_location(
        self, app, client, buckets
    ):
        set_auth(app, admin_user())
        ids = {
            "eu-west": await _workspace(client, "loc-eu", "eu-west"),
            "us-east": await _workspace(client, "loc-us", "us-east"),
            "default": await _workspace(client, "loc-home", None),
        }
        # Cold cache, as on a replica that did not create them: every route is a real query.
        get_storage().resolver._known.clear()
        for ws_id in ids.values():
            await _upload_state(client, ws_id, 1)
            await _upload_config(client, ws_id)
            await _write_run_objects(ws_id)

        found = {name: await _keys(store) for name, store in buckets.items()}
        found["default"] = await _keys(get_storage().default)
        for name, ws_id in ids.items():
            mine = _owned(found[name], ws_id)
            assert {k.split("/", 1)[0] for k in mine} == {"state", "config", "logs", "plans"}
            for other, keys_there in found.items():
                if other != name:
                    assert not _owned(keys_there, ws_id), f"{name} objects found in {other}"

        state = next(k for k in found["eu-west"] if k.startswith(f"state/{ids['eu-west']}/"))
        assert json.loads(await get_storage().get(state))["serial"] == 1
        assert get_storage().resolver._known == {
            ids["eu-west"]: "eu-west",
            ids["us-east"]: "us-east",
            ids["default"]: "default",
        }


class TestADeletedWorkspaceIsFoundInItsLocation:
    async def _delete(self, client, ws_id: str) -> None:
        r = await client.delete(f"{NATIVE}/ws-{ws_id}", headers=AUTH)
        assert r.status_code == 204, r.text
        # The row is gone and nothing is cached: reads must find the objects by fanning out.
        get_storage().resolver._known.clear()
        assert await get_storage().resolver.location_for(ws_id) is None

    async def test_a_restore_reads_and_writes_the_original_location(self, app, client, buckets):
        set_auth(app, admin_user())
        old = await _workspace(client, "loc-restore", "eu-west")
        for serial in (1, 2):
            await _upload_state(client, old, serial)
        await self._delete(client, old)

        r = await client.post(f"/api/v1/deleted-workspaces/{old}/restore", headers=AUTH)
        assert r.status_code == 201, r.text
        assert r.json()["data"]["attributes"]["state-versions-restored"] == 2
        new = r.json()["data"]["id"].removeprefix("ws-")

        assert len(_owned(await _keys(buckets["eu-west"]), new)) == 2
        assert not _owned(await _keys(buckets["us-east"]), new)
        assert not _owned(await _keys(get_storage().default), new)
        assert keys.deleted_workspace_marker_key(old) in await _keys(get_storage().default)

    async def test_the_reaper_removes_expired_state_from_its_location(self, app, client, buckets):
        set_auth(app, admin_user())
        gone = await _workspace(client, "loc-reap", "us-east")
        await _upload_state(client, gone, 1)
        await self._delete(client, gone)
        store = get_storage()
        marker = await dws.read_marker(store, gone)
        marker["deleted_at"] = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        await dws.write_marker(store, gone, marker)
        assert _owned(await _keys(buckets["us-east"]), gone)

        async with get_db_session() as db:
            assert await _cleanup_deleted_workspaces(db, store, 30, 100) == 1

        assert not _owned(await _keys(buckets["us-east"]), gone)
        assert keys.deleted_workspace_marker_key(gone) not in await _keys(store.default)


async def test_a_workspace_written_to_before_its_row_commits_routes_by_the_flushed_row(
    app, monkeypatch
):
    """A catalog provision uploads its configuration inside the creating transaction."""
    from tests.integration.test_catalog_lifecycle_integration import (
        _provision_root,
        _root_catalog,
        _root_version,
    )

    async with get_db_session() as db:
        module, item, pool = await _root_catalog(db)
        await _root_version(db, module, "1.0.0", 'output "v" { value = 1 }')
        ws = await _provision_root(db, item, pool, monkeypatch)
        ws_id = str(ws.id)

    found = _owned(await _keys(get_storage().default), ws_id)
    assert {k.split("/", 1)[0] for k in found} == {"config"}


async def _rows(*locations: str | None) -> list[str]:
    from terrapod.db.models import Workspace

    async with get_db_session() as db:
        made = [
            Workspace(name=f"row-{uuid.uuid4().hex[:8]}", storage_location=loc) for loc in locations
        ]
        db.add_all(made)
        await db.flush()
        return [str(ws.id) for ws in made]


class TestAgainstPostgres:
    async def test_the_resolver_query(self, app):
        eu, home = await _rows("eu-west", None)
        resolver = DbLocationResolver()

        assert await resolver.location_for(eu) == "eu-west"
        assert await resolver.location_for(home) == "default"
        assert await resolver.location_for(str(uuid.uuid4())) is None
        assert resolver._known == {eu: "eu-west", home: "default"}

    async def test_the_startup_check(self, app, monkeypatch):
        from terrapod.storage.location_resolver import check_locations_at_startup

        await check_locations_at_startup()
        await _rows("eu-west", None)
        await check_locations_at_startup()
        await _rows("removed")
        with pytest.raises(RuntimeError, match="not configured: removed"):
            await check_locations_at_startup()
        monkeypatch.setattr(settings.storage, "locations", {})
        with pytest.raises(RuntimeError, match="none are configured"):
            await check_locations_at_startup()

    async def test_the_follower_guard_counts_rows_naming_a_location(self, app):
        from terrapod.services import blob_sync

        await _rows(None)
        assert await blob_sync._rows_naming_a_location() == 0
        await _rows("eu-west", "us-east")
        assert await blob_sync._rows_naming_a_location() == 2

    async def test_a_literal_default_is_not_a_location(self, app, monkeypatch):
        from terrapod.storage.location_resolver import check_locations_at_startup

        monkeypatch.setattr(settings.storage, "locations", {})
        await _rows("default", None)
        await check_locations_at_startup()

    @pytest.mark.parametrize(
        "sql", ["SELECT no_such_column FROM workspaces", "SELECT 1 FROM no_such_table"]
    )
    async def test_postgres_reports_a_pre_migration_schema_by_sqlstate(self, app, sql):
        from sqlalchemy import text

        from terrapod.storage.location_resolver import _is_pre_migration

        with pytest.raises(Exception) as caught:
            async with get_db_session() as db:
                await db.execute(text(sql))
        assert _is_pre_migration(caught.value)
