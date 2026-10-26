"""Bounded retries for OCI manifest, tag, and streamed blob requests."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from terrapod import http_retry
from terrapod.config import OCIUpstreamConfig, settings
from terrapod.services.oci import pullthrough_service
from terrapod.services.oci.names import Digest

pytestmark = pytest.mark.asyncio

HOST = "registry.test"
UPSTREAM = "https://registry.test"
REPOSITORY = "team/image"
BLOB = b"layer contents"
BLOB_DIGEST = Digest("sha256", hashlib.sha256(BLOB).hexdigest())
MANIFEST = b'{"schemaVersion":2}'


@pytest.fixture
def point_at_upstream(monkeypatch) -> None:
    monkeypatch.setattr(
        settings.registry.oci,
        "upstreams",
        [OCIUpstreamConfig(host=HOST, api_url=UPSTREAM)],
    )
    monkeypatch.setattr(settings.registry, "cache_only", False)
    monkeypatch.setattr(http_retry, "_backoff_seconds", lambda *_: 0.0)


def _client(responses: list[httpx.Response], calls: list[httpx.Request]) -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


class _MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_stream(self, key: str, chunks) -> None:
        data = bytearray()
        async for chunk in chunks:
            data.extend(chunk)
        self.objects[key] = bytes(data)


class _FailAfterFirstChunk(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    async def __aiter__(self):
        yield b"partial"
        raise httpx.ReadError("connection reset while reading blob", request=self.request)

    async def aclose(self) -> None:
        return None


async def test_manifest_retries_a_5xx(point_at_upstream) -> None:
    calls: list[httpx.Request] = []
    async with _client(
        [
            httpx.Response(503),
            httpx.Response(
                200,
                content=MANIFEST,
                headers={"content-type": "application/vnd.oci.image.manifest.v1+json"},
            ),
        ],
        calls,
    ) as client:
        body, media_type, digest = await pullthrough_service.fetch_manifest(
            HOST, REPOSITORY, "latest", client=client
        )

    assert len(calls) == 2
    assert body == MANIFEST
    assert media_type == "application/vnd.oci.image.manifest.v1+json"
    assert digest == f"sha256:{hashlib.sha256(MANIFEST).hexdigest()}"


async def test_manifest_404_is_final(point_at_upstream) -> None:
    calls: list[httpx.Request] = []
    async with _client([httpx.Response(404), httpx.Response(200)], calls) as client:
        with pytest.raises(pullthrough_service.UpstreamUnavailable):
            await pullthrough_service.fetch_manifest(HOST, REPOSITORY, "missing", client=client)

    assert len(calls) == 1


async def test_tag_digest_revalidation_retries_a_5xx(point_at_upstream) -> None:
    expected = "sha256:" + "ab" * 32
    calls: list[httpx.Request] = []
    async with _client(
        [
            httpx.Response(503),
            httpx.Response(200, headers={"Docker-Content-Digest": expected}),
        ],
        calls,
    ) as client:
        actual = await pullthrough_service.current_upstream_digest(
            HOST, REPOSITORY, "latest", client=client
        )

    assert actual == expected
    assert len(calls) == 2
    assert all(call.method == "HEAD" for call in calls)


async def test_tag_digest_404_is_final_and_uses_the_cached_value(point_at_upstream) -> None:
    calls: list[httpx.Request] = []
    async with _client([httpx.Response(404), httpx.Response(200)], calls) as client:
        actual = await pullthrough_service.current_upstream_digest(
            HOST, REPOSITORY, "latest", client=client
        )

    assert actual is None
    assert len(calls) == 1


async def test_tag_digest_revalidation_is_bounded_tighter_than_the_shared_default(
    point_at_upstream, monkeypatch
) -> None:
    """This call site's latency budget is a constraint, not a preference (#2243).

    Revalidation runs inline on the manifest read path, so the shared default --
    four attempts with 1/2/4s of backoff -- would make a pull that could have
    been served from cache wait ~59s against this function's own 3s/10s timeouts,
    where a single shot is ~13s. One extra attempt is deliberate; four is not.

    Asserted here rather than globally because `fetch_manifest` and `fetch_blob`
    deliberately keep the default: they have no cached answer to fall back on, so
    for them a retry is the difference between a served image and a failed pull.

    The delay is checked by RECORDING what the call site passed rather than by
    timing it, because `point_at_upstream` zeroes the backoff -- so a revert to
    `base_delay=1.0` would be invisible to any wall-clock assertion.
    """
    seen: list[tuple] = []

    def _record(*args: float) -> float:
        seen.append(args)
        return 0.0

    monkeypatch.setattr(http_retry, "_backoff_seconds", _record)

    calls: list[httpx.Request] = []
    async with _client([httpx.Response(503)], calls) as client:
        actual = await pullthrough_service.current_upstream_digest(
            HOST, REPOSITORY, "latest", client=client
        )

    # Gives up and lets the caller serve what it has, rather than raising -- the
    # documented "returns None on any failure" contract is unchanged.
    assert actual is None
    # Two attempts, and strictly fewer than the shared default allows.
    assert len(calls) == 2
    assert len(calls) < http_retry.DEFAULT_RETRIES + 1
    # `_backoff_seconds(attempt, base_delay, max_delay)` -- one sleep, well under
    # the shared base delay.
    assert [args[1] for args in seen] == [0.2]
    assert all(args[1] < http_retry.DEFAULT_BASE_DELAY for args in seen)


async def test_tag_listing_retries_a_5xx(point_at_upstream) -> None:
    calls: list[httpx.Request] = []
    async with _client(
        [httpx.Response(503), httpx.Response(200, json={"tags": ["v1"]})],
        calls,
    ) as client:
        tags = await pullthrough_service.list_tags_at(
            UPSTREAM, REPOSITORY, label=HOST, client=client
        )

    assert tags == ["v1"]
    assert len(calls) == 2


async def test_tag_listing_404_is_an_empty_answer_without_retry(point_at_upstream) -> None:
    calls: list[httpx.Request] = []
    async with _client([httpx.Response(404), httpx.Response(200)], calls) as client:
        tags = await pullthrough_service.list_tags_at(
            UPSTREAM, REPOSITORY, label=HOST, client=client
        )

    assert tags == []
    assert len(calls) == 1


async def test_blob_retries_a_5xx_before_writing_the_verified_object(
    point_at_upstream,
) -> None:
    calls: list[httpx.Request] = []
    storage = _MemoryStore()
    async with _client([httpx.Response(503), httpx.Response(200, content=BLOB)], calls) as client:
        size = await pullthrough_service.fetch_blob(
            HOST, REPOSITORY, BLOB_DIGEST, storage, "blobs/layer", client=client
        )

    assert len(calls) == 2
    assert size == len(BLOB)
    assert storage.objects == {"blobs/layer": BLOB}


async def test_blob_404_is_final_and_leaves_no_object(point_at_upstream) -> None:
    calls: list[httpx.Request] = []
    storage = _MemoryStore()
    async with _client([httpx.Response(404), httpx.Response(200, content=BLOB)], calls) as client:
        with pytest.raises(pullthrough_service.UpstreamUnavailable):
            await pullthrough_service.fetch_blob(
                HOST, REPOSITORY, BLOB_DIGEST, storage, "blobs/layer", client=client
            )

    assert len(calls) == 1
    assert storage.objects == {}


async def test_blob_retries_a_midstream_transport_failure_without_duplicate_bytes(
    point_at_upstream,
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=_FailAfterFirstChunk(request))
        return httpx.Response(200, content=BLOB)

    storage = _MemoryStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        size = await pullthrough_service.fetch_blob(
            HOST, REPOSITORY, BLOB_DIGEST, storage, "blobs/layer", client=client
        )

    assert len(calls) == 2
    assert size == len(BLOB)
    assert storage.objects == {"blobs/layer": BLOB}
