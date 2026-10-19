"""An authenticated upstream must not break the pull-through's read path.

`_auth` returns a ``(username, password)`` tuple, which is what
``httpx.AsyncClient(auth=...)`` wants. `current_upstream_digest` used to put
that tuple into an ``Authorization`` *header* instead, and httpx refuses a
non-string header value with ``TypeError: Header value must be str or bytes``.

That matters more than a wrong header would, for two reasons. The `TypeError`
is not an ``httpx.HTTPError``, so it escaped the handler that exists to make
this function return ``None`` on any failure -- breaking the contract its own
docstring states -- and it escaped ``_revalidate_tag`` and ``get_manifest``
with it, turning an ordinary image pull into a 500. And it fired *only* for an
upstream with credentials configured, so every anonymous mirror worked
perfectly and the defect sat unmet.

`test_oci_tag_revalidation.py` cannot catch this class: every test there patches
``current_upstream_digest`` itself, so the body of it never runs. These drive
the real function, and the second drives a real socket -- because the thing
under test is whether httpx can build the request at all, which is decided
before any transport is involved and is invisible to a mocked-out call.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from terrapod.config import OCIUpstreamConfig, settings
from terrapod.services.oci import pullthrough_service

HOST = "registry.test"
USERNAME = "mirror-reader"
PASSWORD = "s3cret-from-a-secret"
DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
def authenticated_upstream(monkeypatch):
    """An upstream with credentials, which is the only case that ever failed."""
    upstream = OCIUpstreamConfig(host=HOST, api_url=f"http://{HOST}")
    monkeypatch.setattr(settings.registry.oci, "upstreams", [upstream])
    monkeypatch.setattr(settings.registry, "cache_only", False)
    monkeypatch.setenv(upstream.password_env_var, PASSWORD)
    monkeypatch.setattr(
        OCIUpstreamConfig, "username", property(lambda self: USERNAME), raising=False
    )
    return upstream


class TestAnAuthenticatedUpstream:
    async def test_an_unreachable_one_returns_none_rather_than_raising(
        self, authenticated_upstream, monkeypatch
    ) -> None:
        """The regression. Pre-fix this raised TypeError before any connection.

        Port 9 (discard) refuses, so the only way to reach `None` is for httpx
        to have built the request successfully first.
        """
        monkeypatch.setattr(
            settings.registry.oci,
            "upstreams",
            [OCIUpstreamConfig(host=HOST, api_url="http://127.0.0.1:9")],
        )
        result = await pullthrough_service.current_upstream_digest(HOST, "some/repo", "2.21")
        assert result is None

    async def test_the_credential_actually_reaches_upstream(
        self, authenticated_upstream, monkeypatch
    ) -> None:
        """And it is sent as basic auth, not merely sent without raising.

        A real socket, because the assertion is about the bytes httpx puts on
        the wire. Asserting on a mock would re-create the blind spot this file
        exists to close.
        """
        seen: list[bytes] = []

        async def handle(reader, writer):
            seen.append(await reader.read(4096))
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Length: 0\r\n"
                b"Docker-Content-Digest: " + DIGEST.encode() + b"\r\n"
                b"\r\n"
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            settings.registry.oci,
            "upstreams",
            [OCIUpstreamConfig(host=HOST, api_url=f"http://127.0.0.1:{port}")],
        )
        async with server:
            digest = await pullthrough_service.current_upstream_digest(HOST, "some/repo", "2.21")

        assert digest == DIGEST
        assert seen, "upstream was never contacted"
        request = seen[0].decode("latin-1")
        expected = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        assert f"Authorization: Basic {expected}" in request
        assert "HEAD /v2/some/repo/manifests/2.21 " in request
