"""The peer object-store endpoints (#1159).

Two of these matter more than the rest.

**The content endpoint must not become an arbitrary-read primitive.** A peer token
is entitled to read more than a user's — that is the whole reason it is its own
identity class — so the key has to be checked against the class that owns it
before a byte is served. A prefix test alone is what lets a crafted key through.

**A page thinned by the ownership filter is a short page, not the end of the
class.** `state/index.yaml` lives under `state/` and belongs to its own class, so
it is filtered out of the `state` listing — and if `complete`/`cursor` were
derived from what survived that filter, a backfill would stop part-way believing
it had finished, which is precisely the failure this whole phase exists to avoid.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import PeerIdentity, get_peer_identity
from terrapod.api.routers.replication import router
from terrapod.storage.protocol import ObjectMeta, ObjectNotFoundError

BASE = "/api/terrapod/v1/ha/replication/blobs"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/terrapod/v1")
    app.dependency_overrides[get_peer_identity] = lambda: PeerIdentity(
        client_id="peer-b", token_id="tok-1"
    )
    return app


async def _get(path: str):
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def _meta(key: str, size: int = 10) -> ObjectMeta:
    return ObjectMeta(
        key=key,
        size_bytes=size,
        content_type="application/octet-stream",
        etag="etag-1",
        last_modified=datetime(2025, 1, 1, tzinfo=UTC),
    )


class TestListing:
    async def test_a_class_lists_its_objects(self):
        store = AsyncMock()
        store.list_prefix.return_value = [_meta("state/ws-1/sv-1.tfstate", 42)]

        with patch("terrapod.storage.get_storage", return_value=store):
            body = (await _get(f"{BASE}/state")).json()

        assert body["data"][0]["id"] == "state/ws-1/sv-1.tfstate"
        assert body["data"][0]["attributes"]["size-bytes"] == 42
        assert body["data"][0]["attributes"]["etag"] == "etag-1"
        assert body["meta"]["complete"] is True

    async def test_size_comes_back_so_the_diff_needs_no_fetch(self):
        """Keeping the diff cheap is what lets the copy be throttled."""
        store = AsyncMock()
        store.list_prefix.return_value = [_meta("state/a", 1), _meta("state/b", 2)]

        with patch("terrapod.storage.get_storage", return_value=store):
            body = (await _get(f"{BASE}/state")).json()

        assert [e["attributes"]["size-bytes"] for e in body["data"]] == [1, 2]

    async def test_the_cursor_rides_the_storage_cursor(self):
        store = AsyncMock()
        store.list_prefix.return_value = []

        with patch("terrapod.storage.get_storage", return_value=store):
            await _get(f"{BASE}/state?after=state/a&limit=25")

        assert store.list_prefix.await_args.kwargs == {"after": "state/a", "limit": 25}

    async def test_keys_owned_by_a_more_specific_class_are_excluded(self):
        """`state/index.yaml` is its own class. Returning it here would have the
        follower copy it twice and count it twice."""
        store = AsyncMock()
        store.list_prefix.return_value = [
            _meta("state/index.yaml"),
            _meta("state/ws-1/sv-1.tfstate"),
        ]

        with patch("terrapod.storage.get_storage", return_value=store):
            body = (await _get(f"{BASE}/state")).json()

        assert [e["id"] for e in body["data"]] == ["state/ws-1/sv-1.tfstate"]

    async def test_a_page_thinned_by_the_filter_is_not_the_end_of_the_class(self):
        """The trap. If `complete` were derived from what survived the filter, a
        page of two objects where one was filtered out would look short and stop
        the backfill — silently, part-way through state."""
        store = AsyncMock()
        store.list_prefix.return_value = [_meta("state/index.yaml"), _meta("state/a")]

        with patch("terrapod.storage.get_storage", return_value=store):
            body = (await _get(f"{BASE}/state?limit=2")).json()

        assert len(body["data"]) == 1, "the index was filtered out"
        assert body["meta"]["complete"] is False, (
            "the STORE returned a full page, so there is more to come — deriving "
            "this from the filtered count would end the backfill here"
        )
        assert body["meta"]["cursor"] == "state/a", (
            "the cursor must be the last key the store returned, or the next page "
            "re-fetches the filtered-out ones forever"
        )

    async def test_an_unknown_class_is_a_404(self):
        assert (await _get(f"{BASE}/nope")).status_code == 404

    async def test_a_copy_only_class_still_lists(self):
        """`run_logs` cannot be *verified* from rows, but its objects are still
        listable and copyable — the store's own listing is the truth about what is
        there."""
        store = AsyncMock()
        store.list_prefix.return_value = [_meta("logs/ws-1/plans/run-1.log")]

        with patch("terrapod.storage.get_storage", return_value=store):
            body = (await _get(f"{BASE}/run_logs")).json()

        assert body["data"][0]["id"] == "logs/ws-1/plans/run-1.log"


class TestTheContentEndpointIsNotAnArbitraryRead:
    """A peer token reads more than a user's, so this gate is the containment."""

    async def test_an_object_in_the_class_streams(self):
        store = AsyncMock()
        store.head.return_value = _meta("state/ws-1/sv-1.tfstate", 7)

        async def chunks(key, chunk_size=None):
            yield b"payload"

        store.get_stream = chunks

        with patch("terrapod.storage.get_storage", return_value=store):
            resp = await _get(f"{BASE}/state/content?key=state/ws-1/sv-1.tfstate")

        assert resp.status_code == 200
        assert resp.content == b"payload"

    @pytest.mark.parametrize(
        "key",
        [
            "cache/binaries/tofu/1.12.0/linux_amd64",
            "state/../cache/binaries/tofu",
            "../../etc/passwd",
            "/state/ws-1/sv-1.tfstate",
            # Owned by `state_index`, not `state` — right store, wrong class.
            "state/index.yaml",
        ],
    )
    async def test_a_key_outside_the_class_is_refused(self, key):
        store = AsyncMock()

        with patch("terrapod.storage.get_storage", return_value=store):
            resp = await _get(f"{BASE}/state/content?key={key}")

        assert resp.status_code == 403, f"{key!r} was served under class 'state'"
        store.head.assert_not_awaited()
        assert not store.get_stream.await_count, "no bytes may be read before the gate"

    async def test_the_gate_runs_before_any_store_access(self):
        """Refusing after a `head()` would already have leaked existence."""
        store = AsyncMock()

        with patch("terrapod.storage.get_storage", return_value=store):
            await _get(f"{BASE}/state/content?key=cache/binaries/x")

        store.head.assert_not_awaited()

    async def test_a_deleted_object_is_a_404_not_a_failure(self):
        """Normal and expected: the object went between the listing and the
        fetch. The follower skips it and re-diffs next cycle."""
        store = AsyncMock()
        store.head.side_effect = ObjectNotFoundError("state/gone")

        with patch("terrapod.storage.get_storage", return_value=store):
            resp = await _get(f"{BASE}/state/content?key=state/gone")

        assert resp.status_code == 404

    async def test_an_unknown_class_is_a_404(self):
        assert (await _get(f"{BASE}/nope/content?key=state/a")).status_code == 404


class TestPeerOnly:
    def test_both_routes_are_peer_gated(self):
        """Same containment as the rest of the replication surface: a `peer` token
        and nothing else, and no other endpoint accepts one."""
        wanted = {
            "/ha/replication/blobs/{blob_class}",
            "/ha/replication/blobs/{blob_class}/content",
        }
        seen = set()

        for route in router.routes:
            if route.path not in wanted:
                continue
            seen.add(route.path)
            names = [d.call.__name__ for d in route.dependant.dependencies if d.call]
            assert "get_peer_identity" in names, route.path

        assert seen == wanted, f"missing routes: {wanted - seen}"
