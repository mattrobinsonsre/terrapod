"""Transient upstream failures are retried by the package proxy surfaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from terrapod import http_retry
from terrapod.services.package_cache import galaxy, goproxy, npm, nuget, pypi
from terrapod.services.package_cache.substrate import (
    Artifact,
    NotFoundUpstream,
    _fetch_to_file_with_retry,
)

pytestmark = pytest.mark.asyncio

UPSTREAM = "https://upstream.test"
ARTIFACT_BYTES = b"wheel contents after retry"


class _FailAfterFirstChunk(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self.request = request

    async def __aiter__(self):
        yield b"partial wheel contents"
        raise httpx.ReadError("connection reset while reading artifact", request=self.request)

    async def aclose(self) -> None:
        return None


async def _fetch_galaxy(client: httpx.AsyncClient, monkeypatch) -> dict:
    monkeypatch.setattr(galaxy, "upstream_base", lambda: UPSTREAM)
    return await galaxy.fetch_collection("community", "general", client=client)


async def _fetch_pypi(client: httpx.AsyncClient, monkeypatch) -> dict:
    monkeypatch.setattr(pypi, "upstream_base", lambda: UPSTREAM)
    return await pypi.fetch_index("flask", client=client)


async def _fetch_npm(client: httpx.AsyncClient, monkeypatch) -> dict:
    monkeypatch.setattr(npm, "upstream_base", lambda: UPSTREAM)
    return await npm.fetch_packument("left-pad", client=client)


async def _fetch_nuget(client: httpx.AsyncClient, monkeypatch) -> bytes:
    monkeypatch.setattr(nuget, "upstream_base", lambda: UPSTREAM)
    return await nuget.fetch_versions("Newtonsoft.Json", client=client)


async def _fetch_goproxy(client: httpx.AsyncClient, monkeypatch) -> bytes:
    monkeypatch.setattr(goproxy, "upstream_base", lambda: UPSTREAM)
    return await goproxy.fetch_document("example.com/module", "@latest", client=client)


SURFACES = [
    ("Galaxy", _fetch_galaxy, {"data": [], "links": {}}),
    ("PyPI", _fetch_pypi, {"meta": {"api-version": "1.0"}, "files": []}),
    ("npm", _fetch_npm, {"name": "left-pad", "versions": {}}),
    ("NuGet", _fetch_nuget, {"versions": ["1.0.0"]}),
    ("Go module proxy", _fetch_goproxy, {"Version": "v1.0.0"}),
]


@pytest.fixture
def zero_retry_delay(monkeypatch) -> None:
    """Keep retry tests deterministic without waiting through production backoff."""
    monkeypatch.setattr(http_retry, "_backoff_seconds", lambda *_: 0.0)


@pytest.mark.parametrize(("surface", "fetch", "body"), SURFACES)
async def test_proxy_retries_a_5xx_before_returning_metadata(
    surface: str,
    fetch: Callable[..., Awaitable[object]],
    body: dict,
    monkeypatch,
    zero_retry_delay,
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await fetch(client, monkeypatch)

    assert result is not None, surface
    assert len(calls) == 2, surface


@pytest.mark.parametrize(("surface", "fetch", "_body"), SURFACES)
async def test_proxy_does_not_retry_a_404(
    surface: str,
    fetch: Callable[..., Awaitable[object]],
    _body: dict,
    monkeypatch,
    zero_retry_delay,
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(NotFoundUpstream):
            await fetch(client, monkeypatch)

    assert len(calls) == 1, surface


async def test_streamed_artifact_restarts_the_temp_file_after_a_read_failure(
    tmp_path, zero_retry_delay
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=_FailAfterFirstChunk(request))
        return httpx.Response(200, content=ARTIFACT_BYTES)

    artifact = Artifact(
        ecosystem="pypi",
        name="example",
        version="1.0",
        filename="example-1.0.whl",
        upstream_url=UPSTREAM,
    )
    path = tmp_path / artifact.filename

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await _fetch_to_file_with_retry(client, artifact, str(path))

    assert len(calls) == 2
    assert path.read_bytes() == ARTIFACT_BYTES
