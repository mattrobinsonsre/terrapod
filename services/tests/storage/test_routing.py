"""The routing store sends workspace-scoped objects to the workspace's location."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from terrapod.config import S3Config, StorageBackend, StorageLocationConfig, settings
from terrapod.storage import keys
from terrapod.storage.keys import WORKSPACE_KEY_PREFIXES
from terrapod.storage.protocol import (
    InstrumentedStore,
    ObjectMeta,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectStorePermissionError,
    PresignedURL,
)
from terrapod.storage.routing import RoutingStore, StaticDefaultResolver
from tests.storage.test_keys_workspace_scope import _build, _shared_builders, _workspace_builders

WS_EU = "ws-eu1"
WS_DEFAULT = "ws-home1"
WS_UNKNOWN = "ws-ghost1"


class FakeStore:
    """In-memory store recording every call, with distinct URL and expiry markers."""

    def __init__(self, name: str, expiry: int) -> None:
        self.name = name
        self.expiry = expiry
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.closed = 0
        self.streams_closed = 0

    def _meta(self, key: str) -> ObjectMeta:
        return ObjectMeta(key, len(self.objects[key]), "x", "e", datetime(2026, 1, 1, tzinfo=UTC))

    async def put(self, key, data, content_type="application/octet-stream", metadata=None):
        self.calls.append(("put", key))
        self.objects[key] = data
        return self._meta(key)

    async def put_stream(self, key, chunks, content_type="application/octet-stream", metadata=None):
        self.calls.append(("put_stream", key))
        self.objects[key] = b"".join([c async for c in chunks])
        return self._meta(key)

    async def get(self, key):
        self.calls.append(("get", key))
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def get_stream(self, key, chunk_size=256 * 1024) -> AsyncIterator[bytes]:
        self.calls.append(("get_stream", key))
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        data = self.objects[key]
        try:
            for i in range(0, len(data), 2):
                yield data[i : i + 2]
        finally:
            self.streams_closed += 1

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.objects.pop(key, None)

    async def exists(self, key):
        self.calls.append(("exists", key))
        return key in self.objects

    async def head(self, key):
        self.calls.append(("head", key))
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self._meta(key)

    async def list_prefix(self, prefix, *, after="", limit=None):
        self.calls.append(("list_prefix", prefix))
        found = sorted(k for k in self.objects if k.startswith(prefix) and k > after)
        return [self._meta(k) for k in found[:limit]]

    def _url(self, verb, key, expiry_seconds):
        expiry = self.expiry if expiry_seconds is None else expiry_seconds
        expires = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=expiry)
        return PresignedURL(url=f"{self.name}://{verb}/{key}", expires_at=expires)

    async def presigned_get_url(self, key, expiry_seconds=None):
        self.calls.append(("presigned_get_url", key))
        return self._url("get", key, expiry_seconds)

    async def presigned_put_url(
        self, key, content_type="application/octet-stream", expiry_seconds=None
    ):
        self.calls.append(("presigned_put_url", key))
        return self._url("put", key, expiry_seconds)

    async def close(self):
        self.closed += 1


class MapResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    async def location_for(self, workspace_id: str) -> str | None:
        return self.mapping.get(workspace_id)


@pytest.fixture
def stores() -> tuple[FakeStore, FakeStore]:
    return FakeStore("home", 3600), FakeStore("eu", 900)


@pytest.fixture
def router(stores) -> RoutingStore:
    home, eu = stores
    resolver = MapResolver({WS_EU: "eu-west", WS_DEFAULT: "default"})
    return RoutingStore(default=home, locations={"eu-west": eu}, resolver=resolver)


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for p in parts:
        yield p


def test_the_routing_store_satisfies_the_protocol(router):
    assert isinstance(router, ObjectStore)


class TestWrites:
    async def test_every_derived_workspace_key_lands_on_its_location(self, router, stores):
        home, eu = stores
        names = sorted(_workspace_builders())
        assert len(names) >= 15
        for name in names:
            key = _build(name, WS_EU)
            await router.put(key, b"x")
            assert key in eu.objects and key not in home.objects, f"{name} -> {key}"

    async def test_every_shared_key_lands_on_default(self, router, stores):
        home, eu = stores
        for name in sorted(_shared_builders()):
            key = _build(name, None)
            await router.put(key, b"x")
            assert key in home.objects and key not in eu.objects, f"{name} -> {key}"
        for key in (keys.state_index_key(), keys.deleted_workspace_marker_key(WS_EU)):
            await router.put(key, b"x")
            assert key in home.objects and key not in eu.objects, key

    async def test_a_workspace_resolved_to_default_lands_on_default(self, router, stores):
        home, eu = stores
        key = keys.state_key(WS_DEFAULT, "sv-1")
        await router.put_stream(key, _chunks(b"a", b"b"))
        assert home.objects[key] == b"ab" and not eu.objects

    async def test_put_stream_and_presigned_put_route_by_key(self, router, stores):
        home, eu = stores
        key = keys.config_version_key(WS_EU, "cv-1")
        await router.put_stream(key, _chunks(b"a", b"b"))
        url = await router.presigned_put_url(key, "application/gzip")
        assert eu.objects[key] == b"ab"
        assert url.url == f"eu://put/{key}"
        assert not home.calls

    @pytest.mark.parametrize("op", ["put", "put_stream", "presigned_put_url"])
    async def test_an_unknown_workspace_write_raises_and_writes_nowhere(self, router, stores, op):
        home, eu = stores
        key = keys.state_key(WS_UNKNOWN, "sv-1")
        with pytest.raises(ObjectStoreError):
            if op == "put":
                await router.put(key, b"x")
            elif op == "put_stream":
                await router.put_stream(key, _chunks(b"x"))
            else:
                await router.presigned_put_url(key)
        assert not home.calls and not eu.calls

    async def test_a_resolved_location_missing_from_config_raises(self, stores):
        home, eu = stores
        router = RoutingStore(home, {"eu-west": eu}, MapResolver({WS_EU: "gone"}))
        with pytest.raises(ObjectStoreError):
            await router.put(keys.state_key(WS_EU, "sv-1"), b"x")
        with pytest.raises(ObjectStoreError):
            await router.get(keys.state_key(WS_EU, "sv-1"))
        assert not home.calls and not eu.calls


class TestReads:
    async def test_a_known_workspace_reads_only_its_store(self, router, stores):
        home, eu = stores
        key = keys.plan_log_key(WS_EU, "run-1")
        eu.objects[key] = b"hello"
        assert await router.get(key) == b"hello"
        assert b"".join([c async for c in router.get_stream(key)]) == b"hello"
        assert (await router.head(key)).key == key
        assert await router.exists(key)
        assert not home.calls

    async def test_an_unknown_workspace_read_finds_a_non_default_object(self, router, stores):
        home, eu = stores
        key = keys.state_key(WS_UNKNOWN, "sv-1")
        eu.objects[key] = b"hello"
        assert await router.get(key) == b"hello"
        assert b"".join([c async for c in router.get_stream(key)]) == b"hello"
        assert (await router.head(key)).key == key
        assert await router.exists(key)
        assert (await router.presigned_get_url(key)).url == f"eu://get/{key}"

    async def test_an_unknown_workspace_object_found_nowhere_is_not_found(self, router):
        key = keys.state_key(WS_UNKNOWN, "sv-1")
        with pytest.raises(ObjectNotFoundError):
            await router.get(key)
        with pytest.raises(ObjectNotFoundError):
            [c async for c in router.get_stream(key)]
        with pytest.raises(ObjectNotFoundError):
            await router.head(key)
        assert not await router.exists(key)

    async def test_an_empty_object_streams_from_the_store_that_has_it(self, router, stores):
        _, eu = stores
        key = keys.state_key(WS_UNKNOWN, "sv-1")
        eu.objects[key] = b""
        assert [c async for c in router.get_stream(key)] == []

    @pytest.mark.parametrize("ws", [WS_EU, WS_UNKNOWN])
    async def test_a_stream_stopped_early_closes_the_inner_stream(self, router, stores, ws):
        _, eu = stores
        key = keys.state_key(ws, "sv-1")
        eu.objects[key] = b"abcdef"
        stream = router.get_stream(key)
        assert await anext(stream) == b"ab"
        await stream.aclose()
        assert eu.streams_closed == 1

    async def test_an_unknown_workspace_delete_removes_from_every_store(self, router, stores):
        home, eu = stores
        key = keys.state_key(WS_UNKNOWN, "sv-1")
        home.objects[key] = eu.objects[key] = b"x"
        await router.delete(key)
        assert key not in home.objects and key not in eu.objects

    async def test_a_known_workspace_delete_touches_only_its_store(self, router, stores):
        home, eu = stores
        key = keys.state_key(WS_EU, "sv-1")
        eu.objects[key] = b"x"
        await router.delete(key)
        assert key not in eu.objects and not home.calls


class TestPresignedUrls:
    async def test_presigned_urls_come_from_the_owning_store(self, router, stores):
        key_eu = keys.state_key(WS_EU, "sv-1")
        key_home = keys.state_index_key()
        eu_get = await router.presigned_get_url(key_eu)
        home_get = await router.presigned_get_url(key_home)
        assert eu_get.url == f"eu://get/{key_eu}"
        assert home_get.url == f"home://get/{key_home}"
        assert (eu_get.expires_at - datetime(2026, 1, 1, tzinfo=UTC)).total_seconds() == 900
        assert (home_get.expires_at - datetime(2026, 1, 1, tzinfo=UTC)).total_seconds() == 3600

    async def test_an_explicit_expiry_is_passed_through(self, router):
        url = await router.presigned_put_url(keys.state_key(WS_EU, "sv-1"), expiry_seconds=60)
        assert (url.expires_at - datetime(2026, 1, 1, tzinfo=UTC)).total_seconds() == 60


class TestListPrefix:
    @pytest.fixture
    def seeded(self, stores):
        home, eu = stores
        for k in ("state/a/1", "state/c/1", "state/e/1", "state/index.yaml"):
            home.objects[k] = b"x"
        for k in ("state/b/1", "state/d/1", "state/f/1"):
            eu.objects[k] = b"x"
        home.objects["plans/a/1"] = b"x"
        return stores

    async def test_a_shared_prefix_merges_every_store_in_key_order(self, router, seeded):
        got = [m.key for m in await router.list_prefix("state/")]
        expected = ["state/a/1", "state/b/1", "state/c/1", "state/d/1", "state/e/1", "state/f/1"]
        assert got == [*expected, "state/index.yaml"]

    async def test_the_merge_honours_after_and_limit(self, router, seeded):
        got = [m.key for m in await router.list_prefix("state/", after="state/b/1", limit=3)]
        assert got == ["state/c/1", "state/d/1", "state/e/1"]

    async def test_paging_the_merge_walks_every_key_once(self, router, seeded):
        seen: list[str] = []
        after = ""
        while page := await router.list_prefix("state/", after=after, limit=2):
            seen += [m.key for m in page]
            after = page[-1].key
        assert seen == sorted(seen) and len(seen) == len(set(seen)) == 7

    async def test_a_workspace_prefix_lists_only_its_store(self, router, stores):
        home, eu = stores
        eu.objects[keys.state_key(WS_EU, "sv-1")] = b"x"
        got = await router.list_prefix(f"state/{WS_EU}/")
        assert [m.key for m in got] == [keys.state_key(WS_EU, "sv-1")]
        assert not home.calls

    @pytest.mark.parametrize(
        "prefix",
        ["", "s", "state", "state/", "plans/", f"state/{WS_EU}", keys.PULUMI_CHECKPOINT_PREFIX],
    )
    async def test_a_prefix_without_a_whole_workspace_segment_merges(self, router, stores, prefix):
        home, eu = stores
        await router.list_prefix(prefix)
        assert ("list_prefix", prefix) in home.calls and ("list_prefix", prefix) in eu.calls

    async def test_listing_a_shared_prefix_goes_to_default_only(self, router, stores):
        home, eu = stores
        prefixes = {keys.DELETED_MARKER_PREFIX, keys.deleted_workspace_marker_key(WS_EU)}
        for name in _shared_builders():
            key = _build(name, None)
            parts = key.split("/")
            dirs = {"/".join(parts[:i]) + "/" for i in range(1, len(parts))}
            prefixes |= {key} | {
                d for d in dirs if not any(w.startswith(d) for w in WORKSPACE_KEY_PREFIXES)
            }
        assert len(prefixes) >= 30
        for prefix in sorted(prefixes):
            await router.list_prefix(prefix)
        assert not eu.calls, eu.calls
        assert {p for _, p in home.calls} == prefixes

    async def test_an_unknown_workspace_prefix_merges(self, router, stores):
        home, eu = stores
        eu.objects[keys.state_key(WS_UNKNOWN, "sv-1")] = b"x"
        got = await router.list_prefix(f"state/{WS_UNKNOWN}/")
        assert [m.key for m in got] == [keys.state_key(WS_UNKNOWN, "sv-1")]
        assert home.calls and eu.calls


class DeniedStore(FakeStore):
    """A store whose every operation is refused, as a bucket without access would be."""

    def _deny(self, op, key):
        self.calls.append((op, key))
        raise ObjectStorePermissionError(f"denied: {key}")

    async def get(self, key):
        self._deny("get", key)

    async def get_stream(self, key, chunk_size=256 * 1024) -> AsyncIterator[bytes]:
        self._deny("get_stream", key)
        yield b""

    async def exists(self, key):
        self._deny("exists", key)

    async def head(self, key):
        self._deny("head", key)

    async def list_prefix(self, prefix, *, after="", limit=None):
        self._deny("list_prefix", prefix)

    async def presigned_get_url(self, key, expiry_seconds=None):
        self._deny("presigned_get_url", key)

    async def close(self):
        self.closed += 1
        raise ObjectStorePermissionError("denied: close")


async def _read(router: RoutingStore, op: str, key: str, prefix: str = "state/"):
    if op == "get_stream":
        return [c async for c in router.get_stream(key)]
    if op == "list_prefix":
        return await router.list_prefix(prefix)
    return await getattr(router, op)(key)


_READS = ["get", "head", "get_stream", "exists", "presigned_get_url", "list_prefix"]


class TestARealErrorIsNeverAMiss:
    @pytest.mark.parametrize("op", _READS)
    async def test_an_unknown_workspace_fan_out_raises_a_denied_store(self, op):
        denied, eu = DeniedStore("home", 3600), FakeStore("eu", 900)
        key = keys.state_key(WS_UNKNOWN, "sv-1")
        eu.objects[key] = b"x"
        router = RoutingStore(denied, {"eu-west": eu}, MapResolver({}))
        with pytest.raises(ObjectStorePermissionError):
            await _read(router, op, key)

    @pytest.mark.parametrize("op", _READS)
    async def test_a_denied_owning_store_raises_without_falling_back(self, op):
        home, denied = FakeStore("home", 3600), DeniedStore("eu", 900)
        key = keys.state_key(WS_EU, "sv-1")
        home.objects[key] = b"x"
        router = RoutingStore(home, {"eu-west": denied}, MapResolver({WS_EU: "eu-west"}))
        with pytest.raises(ObjectStorePermissionError):
            await _read(router, op, key, prefix=f"state/{WS_EU}/")
        assert not home.calls


async def test_close_closes_each_store_once(stores):
    home, eu = stores
    router = RoutingStore(home, {"eu-west": eu, "alias": eu}, StaticDefaultResolver())
    await router.close()
    assert home.closed == 1 and eu.closed == 1


async def test_close_keeps_going_after_a_store_fails_and_reraises():
    denied, eu = DeniedStore("home", 3600), FakeStore("eu", 900)
    router = RoutingStore(denied, {"eu-west": eu}, StaticDefaultResolver())
    with pytest.raises(ObjectStorePermissionError):
        await router.close()
    assert denied.closed == 1 and eu.closed == 1


async def test_the_static_resolver_puts_every_workspace_on_default():
    assert await StaticDefaultResolver().location_for(WS_EU) == "default"


class TestInitStorage:
    @pytest.fixture
    def fs_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings.storage, "backend", StorageBackend.FILESYSTEM)
        monkeypatch.setattr(settings.storage.filesystem, "root_dir", str(tmp_path))
        monkeypatch.setattr(settings.storage.filesystem, "hmac_secret", "s")
        monkeypatch.setattr(settings.storage, "locations", {})
        monkeypatch.setattr(settings.metrics, "enabled", False)

    @pytest.mark.parametrize("metrics", [False, True])
    async def test_without_locations_get_storage_is_the_plain_store(
        self, fs_settings, monkeypatch, metrics
    ):
        from terrapod.storage import close_storage, get_storage, init_storage
        from terrapod.storage.filesystem import FilesystemStore

        monkeypatch.setattr(settings.metrics, "enabled", metrics)
        await init_storage()
        try:
            store = get_storage()
            if metrics:
                assert type(store) is InstrumentedStore
                store = store._inner
            assert type(store) is FilesystemStore
        finally:
            await close_storage()

    @pytest.mark.parametrize("metrics", [False, True])
    async def test_with_locations_a_routing_store_is_installed(
        self, fs_settings, monkeypatch, metrics
    ):
        from terrapod.storage import close_storage, get_storage, init_storage
        from terrapod.storage.filesystem import FilesystemStore
        from terrapod.storage.s3 import S3Store

        loc = StorageLocationConfig(
            backend=StorageBackend.S3,
            s3=S3Config(bucket="eu-bucket", presigned_url_expiry_seconds=900),
        )
        monkeypatch.setattr(settings.storage, "locations", {"eu-west": loc})
        monkeypatch.setattr(settings.metrics, "enabled", metrics)
        await init_storage()
        try:
            store = get_storage()
            assert isinstance(store, RoutingStore)
            assert set(store.locations) == {"eu-west"}
            inner = [store.default, store.locations["eu-west"]]
            if metrics:
                assert all(isinstance(s, InstrumentedStore) for s in inner)
                inner = [s._inner for s in inner]
            assert type(inner[0]) is FilesystemStore
            assert type(inner[1]) is S3Store
            assert inner[1]._bucket == "eu-bucket" and inner[1]._default_expiry == 900
            assert isinstance(store.resolver, StaticDefaultResolver)
        finally:
            await close_storage()

    @pytest.mark.parametrize("configured", [False, True])
    async def test_unassigned_locations_are_warned_about_once(
        self, fs_settings, monkeypatch, configured
    ):
        import terrapod.storage as storage_mod

        if configured:
            loc = StorageLocationConfig(backend=StorageBackend.S3, s3=S3Config(bucket="eu"))
            monkeypatch.setattr(settings.storage, "locations", {"eu-west": loc})
        with patch.object(storage_mod, "logger") as log:
            await storage_mod.init_storage()
            await storage_mod.close_storage()
        warnings = [c for c in log.warning.call_args_list if "no workspace" in c.args[0]]
        assert len(warnings) == (1 if configured else 0)
        if configured:
            assert warnings[0].kwargs["locations"] == ["eu-west"]
