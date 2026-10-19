"""Listing an upstream repository's tags, and what "unknown" has to mean.

`upstream_tags` feeds the execution-environment availability read (#2154), and
that read decides whether a workspace write naming an ansible-core line is
refused. So the three outcomes are not interchangeable:

* a list  -- authoritative; a line absent from it does not exist upstream
* ``[]``  -- also authoritative; the repository has no tags
* ``None`` -- **cannot tell**; the caller must accept rather than refuse

Everything here exists to pin that third case to exactly the circumstances that
warrant it, including the one that is easy to get wrong: a listing cut short by
pagination is unknown, not "most of the answer". A tag missing from a partial
listing is indistinguishable from a tag that was never there, and treating a
partial listing as complete would refuse legitimate workspace writes.

Driven over a real socket. The thing under test is what httpx puts on the wire
and what it does with a `Link` header, which a mocked client would simply assert
back at us.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from terrapod.config import OCIUpstreamConfig, settings
from terrapod.services.oci import pullthrough_service

HOST = "registry.test"
REPO = "mattrobinsonsre/terrapod-ee"


def _response(status: str, body: dict | str, extra_headers: str = "") -> bytes:
    payload = body if isinstance(body, str) else json.dumps(body)
    encoded = payload.encode()
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        f"{extra_headers}"
        f"\r\n"
    ).encode() + encoded


class _Upstream:
    """A one-shot-per-request HTTP server replaying a scripted list of responses."""

    def __init__(self, responses: list[bytes]) -> None:
        self._responses = responses
        self.requests: list[str] = []
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _Upstream:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        raw = await reader.read(4096)
        self.requests.append(raw.decode("latin-1"))
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        writer.write(self._responses[index])
        await writer.drain()
        writer.close()


@pytest.fixture
def point_at(monkeypatch):
    def _point(port: int) -> None:
        monkeypatch.setattr(
            settings.registry.oci,
            "upstreams",
            [OCIUpstreamConfig(host=HOST, api_url=f"http://127.0.0.1:{port}")],
        )

    return _point


class TestAnAuthoritativeAnswer:
    async def test_a_single_page_returns_its_tags(self, point_at) -> None:
        async with _Upstream([_response("200 OK", {"name": REPO, "tags": ["2.12", "2.21"]})]) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) == ["2.12", "2.21"]
        assert f"GET /v2/{REPO}/tags/list?n=100 " in up.requests[0]

    async def test_pagination_is_followed_and_concatenated(self, point_at) -> None:
        pages = [
            _response(
                "200 OK",
                {"tags": ["2.12"]},
                extra_headers=f'Link: </v2/{REPO}/tags/list?n=1&last=2.12>; rel="next"\r\n',
            ),
            _response("200 OK", {"tags": ["2.21"]}),
        ]
        async with _Upstream(pages) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) == ["2.12", "2.21"]
        assert len(up.requests) == 2
        assert "last=2.12" in up.requests[1]

    async def test_a_missing_repository_is_an_empty_answer_not_unknown(self, point_at) -> None:
        """404 means upstream knows: there are no tags. Distinct from unreachable."""
        async with _Upstream([_response("404 Not Found", {"errors": []})]) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) == []

    async def test_a_null_tags_field_is_empty_rather_than_malformed(self, point_at) -> None:
        """The spec permits `tags: null` for an empty repository."""
        async with _Upstream([_response("200 OK", {"name": REPO, "tags": None})]) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) == []


class TestUnknown:
    """Each of these must be None, because each one could hide a tag that exists."""

    async def test_an_unreachable_upstream(self, point_at) -> None:
        point_at(9)  # discard port: refuses
        assert await pullthrough_service.upstream_tags(HOST, REPO) is None

    async def test_a_server_error(self, point_at) -> None:
        async with _Upstream([_response("503 Service Unavailable", {"errors": []})]) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) is None

    async def test_a_non_json_body(self, point_at) -> None:
        """A proxy error page is a failure to reach the registry, not an empty repo."""
        async with _Upstream([_response("200 OK", "<html>gateway timeout</html>")]) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) is None

    async def test_a_listing_cut_short_by_the_page_cap(self, point_at) -> None:
        """The case worth the whole return type: partial is unknown, not partial.

        Every page advertises another, so the walk can never complete. Returning
        what it collected would look authoritative and refuse writes for every
        line that happened to fall beyond the cap.
        """
        endless = _response(
            "200 OK",
            {"tags": ["2.12"]},
            extra_headers=f'Link: </v2/{REPO}/tags/list?n=1&last=2.12>; rel="next"\r\n',
        )
        async with _Upstream([endless]) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) is None
        assert len(up.requests) == pullthrough_service._MAX_TAG_PAGES


class TestTheLinkHeaderIsNotATrustedRedirect:
    async def test_an_off_host_next_page_is_not_followed(self, point_at) -> None:
        """Otherwise an upstream could walk the listing onto a host of its choosing.

        The first page's tags are still returned: it answered, and refusing to
        follow its Link is not a reason to discard what it said.
        """
        pages = [
            _response(
                "200 OK",
                {"tags": ["2.21"]},
                extra_headers='Link: <https://elsewhere.invalid/v2/x/tags/list>; rel="next"\r\n',
            )
        ]
        async with _Upstream(pages) as up:
            point_at(up.port)
            assert await pullthrough_service.upstream_tags(HOST, REPO) == ["2.21"]
        assert len(up.requests) == 1
