"""
Tests for the filesystem storage backend.

Includes presigned URL endpoint tests via FastAPI test client.
"""

from __future__ import annotations

import time
import urllib.parse

import httpx
import pytest
from fastapi import FastAPI

from terrapod.storage.filesystem import FilesystemStore
from terrapod.storage.filesystem_routes import router, set_filesystem_store
from terrapod.storage.protocol import ObjectNotFoundError, ObjectStoreError


class TestFilesystemStore:
    async def test_put_and_get(self, fs_store: FilesystemStore) -> None:
        data = b"hello world"
        meta = await fs_store.put("test/file.txt", data, content_type="text/plain")
        assert meta.key == "test/file.txt"
        assert meta.size_bytes == len(data)
        assert meta.content_type == "text/plain"

        result = await fs_store.get("test/file.txt")
        assert result == data

    async def test_get_nonexistent_raises(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectNotFoundError):
            await fs_store.get("nonexistent/key")

    async def test_delete_existing(self, fs_store: FilesystemStore) -> None:
        await fs_store.put("to-delete.txt", b"data")
        assert await fs_store.exists("to-delete.txt")

        await fs_store.delete("to-delete.txt")
        assert not await fs_store.exists("to-delete.txt")

    async def test_delete_nonexistent_is_idempotent(self, fs_store: FilesystemStore) -> None:
        await fs_store.delete("never-existed.txt")  # Should not raise

    async def test_exists(self, fs_store: FilesystemStore) -> None:
        assert not await fs_store.exists("nope")
        await fs_store.put("yep", b"data")
        assert await fs_store.exists("yep")

    async def test_head(self, fs_store: FilesystemStore) -> None:
        await fs_store.put(
            "meta-test.bin", b"\x00\x01\x02", content_type="application/octet-stream"
        )
        meta = await fs_store.head("meta-test.bin")
        assert meta.size_bytes == 3
        assert meta.content_type == "application/octet-stream"
        assert meta.etag

    async def test_head_nonexistent_raises(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectNotFoundError):
            await fs_store.head("nonexistent")

    async def test_list_prefix(self, fs_store: FilesystemStore) -> None:
        await fs_store.put("logs/ws1/plan.log", b"plan1")
        await fs_store.put("logs/ws1/apply.log", b"apply1")
        await fs_store.put("logs/ws2/plan.log", b"plan2")
        await fs_store.put("state/ws1/v1.tfstate", b"state")

        results = await fs_store.list_prefix("logs/ws1/")
        keys = [m.key for m in results]
        assert len(keys) == 2
        assert "logs/ws1/plan.log" in keys
        assert "logs/ws1/apply.log" in keys

    async def test_list_prefix_empty(self, fs_store: FilesystemStore) -> None:
        results = await fs_store.list_prefix("nonexistent/")
        assert results == []

    async def test_put_with_metadata(self, fs_store: FilesystemStore) -> None:
        metadata = {"workspace": "ws-123", "run": "run-456"}
        await fs_store.put("with-meta.txt", b"data", metadata=metadata)
        meta = await fs_store.head("with-meta.txt")
        assert meta.metadata["workspace"] == "ws-123"
        assert meta.metadata["run"] == "run-456"

    async def test_put_stream_and_get(self, fs_store: FilesystemStore) -> None:
        async def _chunks():
            yield b"hello "
            yield b"world"

        meta = await fs_store.put_stream("test/streamed.txt", _chunks(), content_type="text/plain")
        assert meta.key == "test/streamed.txt"
        assert meta.size_bytes == 11
        assert meta.content_type == "text/plain"

        result = await fs_store.get("test/streamed.txt")
        assert result == b"hello world"

    async def test_get_stream(self, fs_store: FilesystemStore) -> None:
        await fs_store.put("test/stream-read.txt", b"abcdefghij", content_type="text/plain")
        result = b""
        async for chunk in fs_store.get_stream("test/stream-read.txt", chunk_size=4):
            result += chunk
        assert result == b"abcdefghij"

    async def test_get_stream_nonexistent_raises(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectNotFoundError):
            async for _ in fs_store.get_stream("nonexistent/key"):
                pass  # pragma: no cover

    async def test_path_traversal_rejected(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectStoreError):
            await fs_store.put("../escape.txt", b"nope")

    async def test_absolute_path_rejected(self, fs_store: FilesystemStore) -> None:
        with pytest.raises(ObjectStoreError):
            await fs_store.put("/etc/passwd", b"nope")


class TestFilesystemPresignedURLs:
    async def test_presigned_get_url(self, fs_store: FilesystemStore) -> None:
        url = await fs_store.presigned_get_url("test/key")
        assert "sig=" in url.url
        assert "expires=" in url.url
        assert url.expires_at

    async def test_presigned_put_url(self, fs_store: FilesystemStore) -> None:
        url = await fs_store.presigned_put_url("test/key", content_type="text/plain")
        assert "sig=" in url.url
        assert "content_type=" in url.url
        assert url.headers["Content-Type"] == "text/plain"

    async def test_signature_verification(self, fs_store: FilesystemStore) -> None:
        expires = int(time.time()) + 3600
        sig = fs_store._sign("GET", "test/key", expires)
        assert fs_store.verify_signature("GET", "test/key", str(expires), sig)

    async def test_expired_signature_rejected(self, fs_store: FilesystemStore) -> None:
        expires = int(time.time()) - 10  # Already expired
        sig = fs_store._sign("GET", "test/key", expires)
        assert not fs_store.verify_signature("GET", "test/key", str(expires), sig)

    async def test_wrong_operation_rejected(self, fs_store: FilesystemStore) -> None:
        expires = int(time.time()) + 3600
        sig = fs_store._sign("GET", "test/key", expires)
        assert not fs_store.verify_signature("PUT", "test/key", str(expires), sig)


class TestFilesystemRoutes:
    """Test the presigned URL FastAPI endpoints."""

    @pytest.fixture
    def app(self, fs_store: FilesystemStore) -> FastAPI:
        """Create a test FastAPI app with filesystem routes.

        Mounts under both the canonical /api/terrapod/v1 prefix (which is
        what `filesystem.py` emits in presigned URLs) and the deprecated
        /api/v2 alias, mirroring `app.py`'s `include_moved` behaviour.
        """
        test_app = FastAPI()
        set_filesystem_store(fs_store)
        # Mirrors production (#1529): canonical, the deprecated alias, and the
        # TFE surface. The canonical mount matters here because presigned URLs
        # are generated with that prefix — mounting only the alias would 404 on
        # a URL the store had just handed out.
        test_app.include_router(router, prefix="/api/v1")
        test_app.include_router(router, prefix="/api/terrapod/v1")
        test_app.include_router(router, prefix="/api/v2")
        return test_app

    async def test_presigned_urls_stay_on_the_prefix_runners_recognise(
        self, fs_store: FilesystemStore
    ) -> None:
        """Deliberately NOT the canonical prefix (#1529).

        These URLs are consumed by a runner Job, which matches them against a
        literal compiled into the image it was built with (runner/download.py) to
        decide whether to rewrite the host to the in-cluster API. Runners are
        expected to lag the API, so one in the field has the old matcher —
        emitting the canonical prefix makes it follow the deployment's PUBLIC
        hostname from inside the cluster.

        The failure is silent: an unmatched URL is followed verbatim rather than
        raising, so the first symptom is a run that cannot fetch its config.

        Flips with LAGGING_CONSUMER_PREFIX once the support window closes;
        tests/runner/test_download.py asserts the matcher accepts whatever this
        emits, so the two cannot drift apart meanwhile.
        """
        from terrapod.api.prefixes import LAGGING_CONSUMER_PREFIX

        put_url = await fs_store.presigned_put_url("prefix-check.txt")
        get_url = await fs_store.presigned_get_url("prefix-check.txt")
        assert f"{LAGGING_CONSUMER_PREFIX}/storage/put/" in put_url.url, put_url.url
        assert f"{LAGGING_CONSUMER_PREFIX}/storage/get/" in get_url.url, get_url.url

    async def test_put_and_get_via_routes(self, app: FastAPI, fs_store: FilesystemStore) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Get a presigned PUT URL
            put_url = await fs_store.presigned_put_url("route-test.txt", content_type="text/plain")
            # Extract path + query from the URL
            from urllib.parse import urlparse

            parsed = urlparse(put_url.url)
            path = parsed.path + "?" + parsed.query

            # PUT the data
            resp = await client.put(path, content=b"hello from route test")
            assert resp.status_code == 201

            # Get a presigned GET URL
            get_url = await fs_store.presigned_get_url("route-test.txt")
            parsed = urlparse(get_url.url)
            path = parsed.path + "?" + parsed.query

            # GET the data
            resp = await client.get(path)
            assert resp.status_code == 200
            assert resp.content == b"hello from route test"

    async def test_get_invalid_signature(self, app: FastAPI) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v2/storage/get/test.txt?expires=9999999999&sig=invalid")
            assert resp.status_code == 403

    async def test_get_nonexistent_object(self, app: FastAPI, fs_store: FilesystemStore) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            get_url = await fs_store.presigned_get_url("does-not-exist.txt")
            from urllib.parse import urlparse

            parsed = urlparse(get_url.url)
            path = parsed.path + "?" + parsed.query

            resp = await client.get(path)
            assert resp.status_code == 404


class TestThePresignedKeyIsNotPercentEncoded:
    """A client that re-encodes the URL must still be able to fetch it (#1566).

    The key used to be emitted with `safe=""`, so every `/` became `%2F`. The
    route is `{key:path}` and the signature is over the *decoded* key, so that
    bought nothing — and it cost: npm re-encodes the `%2F` when it follows the
    redirect, the key arrives as a literal `cache%2Fpackages%2F…`, and the
    signature check fails with a 403 that reads like a permissions problem.
    Reproduced against the running stack: the double-encoded URL 403s where both
    the encoded and the decoded form return 200.
    """

    async def test_separators_stay_separators(self, fs_store):
        url = (await fs_store.presigned_get_url("cache/packages/npm/left-pad.tgz")).url
        assert "cache/packages/npm/left-pad.tgz" in url
        assert "%2F" not in url

    async def test_the_same_holds_for_uploads(self, fs_store):
        url = (await fs_store.presigned_put_url("cache/packages/npm/left-pad.tgz")).url
        assert "%2F" not in url

    async def test_a_key_still_round_trips_through_verification(self, fs_store):
        # The signature is over the decoded key either way, so the change must
        # not move what verifies.
        import urllib.parse as up

        key = "cache/packages/npm/left-pad.tgz"
        url = (await fs_store.presigned_get_url(key)).url
        q = up.parse_qs(up.urlparse(url).query)
        assert fs_store.verify_signature("GET", key, q["expires"][0], q["sig"][0])

    async def test_a_character_that_genuinely_needs_encoding_still_is(self, fs_store):
        url = (await fs_store.presigned_get_url("cache/a b/c?d.tgz")).url
        assert "%20" in url and "%3F" in url


class TestAPresignedUploadCannotChooseWhatTheGetServes:
    """The `content_type` parameter sits outside the signature.

    `_sign` covers `operation:key:expires` and nothing else, so an attacker who
    holds a presigned PUT URL (or who tampers with one in flight) can rewrite
    `&content_type=` without breaking it. The value is persisted to the sidecar
    and returned verbatim as the GET's `Content-Type` — from the deployment's own
    origin, with no credential, because the signature *is* the credential. Left
    unconstrained that is stored XSS on the API's hostname.

    It is clamped at the route rather than signed: extending `_sign` would
    invalidate every presigned URL already in flight, PUT and GET alike, since
    both operations share one signing function.
    """

    @pytest.fixture
    def app(self, fs_store: FilesystemStore) -> FastAPI:
        test_app = FastAPI()
        set_filesystem_store(fs_store)
        test_app.include_router(router, prefix="/api/terrapod/v1")
        return test_app

    @staticmethod
    def _route(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.path + "?" + parsed.query

    async def _round_trip(self, app: FastAPI, fs_store: FilesystemStore, declared: str) -> str:
        """Upload declaring `declared`, then return the Content-Type served back."""
        key = "upload-probe.bin"
        put_url = await fs_store.presigned_put_url(key)
        # Appended, not substituted: a second `content_type` is exactly what a
        # tampering client sends, and it must not change the signature's verdict.
        put_route = self._route(put_url.url) + f"&content_type={urllib.parse.quote(declared)}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(put_route, content=b"<script>alert(1)</script>")
            # The upload itself still succeeds — the signature was never broken,
            # which is the whole reason this has to be neutralised on content.
            assert resp.status_code == 201, resp.text

            get_url = await fs_store.presigned_get_url(key)
            resp = await client.get(self._route(get_url.url))
            assert resp.status_code == 200
            return resp.headers["content-type"]

    @pytest.mark.parametrize(
        "declared",
        [
            "text/html",
            "text/html; charset=utf-8",
            "image/svg+xml",
            "application/xhtml+xml",
            "TEXT/HTML",
        ],
    )
    async def test_a_renderable_type_is_never_served_back(
        self, app: FastAPI, fs_store: FilesystemStore, declared: str
    ) -> None:
        served = await self._round_trip(app, fs_store, declared)
        assert served.startswith("application/octet-stream"), (
            f"declared {declared!r} came back as {served!r} — the deployment's own "
            "origin would render attacker-supplied bytes as a document"
        )

    async def test_a_legitimate_type_still_round_trips(
        self, app: FastAPI, fs_store: FilesystemStore
    ) -> None:
        """`application/gzip` is what the one presigned-PUT caller declares.

        A clamp that mis-serves a real artifact has traded one bug for another.
        """
        served = await self._round_trip(app, fs_store, "application/gzip")
        assert served.startswith("application/gzip")

    async def test_every_type_terrapod_stores_survives_the_clamp(self) -> None:
        from terrapod.storage import filesystem_routes as fr

        for allowed in fr._ALLOWED_CONTENT_TYPES:  # noqa: SLF001
            assert fr._safe_content_type(allowed) == allowed  # noqa: SLF001

    async def test_parameters_are_dropped_rather_than_echoed(self) -> None:
        from terrapod.storage import filesystem_routes as fr

        # An allowed bare type with a parameter is honoured, but the parameter
        # is not carried into a response header we control.
        assert fr._safe_content_type("application/json; charset=utf-8") == (  # noqa: SLF001
            "application/json"
        )

    async def test_an_unknown_type_falls_back_rather_than_failing_the_upload(self) -> None:
        from terrapod.storage import filesystem_routes as fr

        # Not an error: a stored artifact is still readable, just not as a
        # type the client named.
        assert fr._safe_content_type("application/x-made-up") == "application/octet-stream"  # noqa: SLF001
        assert fr._safe_content_type("") == "application/octet-stream"  # noqa: SLF001
