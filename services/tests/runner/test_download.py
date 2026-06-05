"""Tests for terrapod.runner.download.

Uses httpx.MockTransport to verify the redirect-aware fetcher
correctly:
  - downloads on a 200,
  - follows a 302 and writes the redirected body,
  - rewrites the redirect hostname when the redirect points at the
    filesystem-backend path,
  - leaves cloud-storage redirect URLs untouched,
  - retries on transient signals (None status, 408, 5xx),
  - fails fast on deterministic 4xx (other than 408).
"""

from __future__ import annotations

import httpx

from terrapod.runner import download as dl


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestDownloadToFile:
    def test_direct_200(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"hello world")

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.example.com/x",
            out,
            client=_client(handler),
        )
        assert result.ok
        assert result.status == 200
        assert out.read_bytes() == b"hello world"

    def test_redirect_followed_to_cloud_storage_url(self, tmp_path) -> None:
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            if request.url.host == "api.example.com":
                return httpx.Response(
                    302,
                    headers={"location": "https://bucket.s3.amazonaws.com/key?sig=abc"},
                )
            return httpx.Response(200, content=b"cloud bytes")

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.example.com/artifact",
            out,
            api_url="https://api.example.com",
            client=_client(handler),
        )
        assert result.ok
        assert out.read_bytes() == b"cloud bytes"
        # Cloud redirect URL is NOT rewritten — second hop went to a
        # host different from the API host. Verified structurally
        # without naming the bucket domain (the test mock's job is to
        # serve content; the assertion's job is to confirm the
        # redirect was followed, not that we matched a hardcoded URL).
        hosts = {httpx.URL(u).host for u in seen_urls}
        api_host = httpx.URL("https://api.example.com").host
        assert hosts - {api_host}, "redirect was not followed to a non-API host"

    def test_filesystem_backend_redirect_hostname_rewritten(self, tmp_path) -> None:
        """A redirect to a different hostname under /api/terrapod/v1/storage/
        is the filesystem backend's signature. Hostname should be
        rewritten to api_url so the runner can reach it from inside
        the cluster."""
        seen_requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append((request.url.host, request.url.path))
            if request.url.path == "/artifact":
                return httpx.Response(
                    302,
                    headers={
                        "location": ("https://terrapod.local/api/terrapod/v1/storage/abc?sig=xyz")
                    },
                )
            # Second hop — the rewritten URL. Serves the artifact.
            return httpx.Response(200, content=b"rewritten")

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.internal/artifact",
            out,
            api_url="https://api.internal",
            client=_client(handler),
        )
        assert result.ok
        assert out.read_bytes() == b"rewritten"
        # Second hop went to api.internal, NOT terrapod.local. Path
        # differentiates the hops; both end up on the same host
        # because the rewriter normalised the redirect target.
        assert all(host == "api.internal" for host, _ in seen_requests)
        assert ("api.internal", "/artifact") in seen_requests
        assert ("api.internal", "/api/terrapod/v1/storage/abc") in seen_requests

    def test_retry_on_5xx_then_success(self, tmp_path) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, content=b"server busy")
            return httpx.Response(200, content=b"ok now")

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.example.com/x",
            out,
            retries=3,
            retry_delay_seconds=0,
            client=_client(handler),
        )
        assert result.ok
        assert attempts["n"] == 2

    def test_no_retry_on_403(self, tmp_path) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(403, content=b"forbidden")

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.example.com/x",
            out,
            retries=3,
            retry_delay_seconds=0,
            client=_client(handler),
        )
        assert not result.ok
        assert result.status == 403
        assert attempts["n"] == 1  # No retry on auth failure

    def test_retry_on_408(self, tmp_path) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(408)
            return httpx.Response(200, content=b"finally")

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.example.com/x",
            out,
            retries=3,
            retry_delay_seconds=0,
            client=_client(handler),
        )
        assert result.ok
        assert attempts["n"] == 3

    def test_404_returns_status_for_caller_to_decide(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.example.com/state",
            out,
            retries=2,
            retry_delay_seconds=0,
            client=_client(handler),
        )
        assert not result.ok
        assert result.status == 404

    def test_storage_4xx_after_redirect_captures_body(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "amazonaws" in request.url.host:
                return httpx.Response(400, content=b"<Error>InvalidArgument</Error>")
            return httpx.Response(
                302,
                headers={"location": "https://bucket.s3.amazonaws.com/key?sig=abc"},
            )

        out = tmp_path / "out.bin"
        result = dl.download_to_file(
            "https://api.example.com/x",
            out,
            api_url="https://api.example.com",
            retries=1,
            retry_delay_seconds=0,
            client=_client(handler),
        )
        assert not result.ok
        assert result.status == 400
        assert "InvalidArgument" in result.body_preview


class TestRedirectRewrite:
    def test_same_host_no_rewrite(self) -> None:
        assert (
            dl._maybe_rewrite_redirect(
                "https://api.example.com/somewhere",
                "https://api.example.com/x",
                "https://api.example.com",
            )
            == "https://api.example.com/somewhere"
        )

    def test_different_host_non_filesystem_no_rewrite(self) -> None:
        assert (
            dl._maybe_rewrite_redirect(
                "https://bucket.s3.amazonaws.com/key?sig=abc",
                "https://api.example.com/x",
                "https://api.example.com",
            )
            == "https://bucket.s3.amazonaws.com/key?sig=abc"
        )

    def test_different_host_filesystem_path_rewritten(self) -> None:
        result = dl._maybe_rewrite_redirect(
            "https://terrapod.local/api/terrapod/v1/storage/abc?sig=xyz",
            "https://api.internal/x",
            "https://api.internal",
        )
        assert result == "https://api.internal/api/terrapod/v1/storage/abc?sig=xyz"

    def test_empty_api_url_passes_through(self) -> None:
        loc = "https://terrapod.local/api/terrapod/v1/storage/abc"
        assert dl._maybe_rewrite_redirect(loc, "https://x", "") == loc
