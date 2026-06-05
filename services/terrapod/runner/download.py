"""Redirect-aware download helper for the runner Job entrypoint.

Port of the tp_curl_download function in docker/runner-entrypoint.sh.

API endpoints return 302 to presigned URLs. For cloud storage (S3,
Azure, GCS) the redirect URL is directly reachable. For the local
filesystem storage backend the redirect points at the public hostname
(e.g. terrapod.local) which may not resolve from inside the cluster.
We rewrite that hostname back to TP_API_URL — but ONLY for paths
under /api/terrapod/v1/storage/ (the filesystem backend signature).
Cloud storage redirect targets are left untouched.

Retry policy matches bash: retry only transient signals — connect
errors, HTTP 000, HTTP 408, HTTP 5xx. Deterministic 4xx (other than
408) fail fast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger("runner.download")


@dataclass
class DownloadResult:
    """Outcome of a download. `ok` mirrors bash success; status is the
    last HTTP code observed (None for connect failures) — same shape
    as TP_LAST_HTTP for callers that need to surface the cause."""

    ok: bool
    status: int | None
    body_preview: str = ""


def _is_filesystem_storage_path(path: str) -> bool:
    """True for the storage backend's filesystem path pattern. Cloud
    storage URLs (*.amazonaws.com et al.) don't match — they're left
    untouched so the runner follows them directly."""
    return path.startswith("/api/terrapod/v1/storage/")


def _maybe_rewrite_redirect(
    location: str,
    request_url: str,
    api_url: str,
) -> str:
    """Rewrite filesystem-backend redirect hostname → api_url.

    Cloud backends produce redirects to *.amazonaws.com et al. which
    must NOT be rewritten. The filesystem backend produces redirects
    to the deployment's public hostname (terrapod.local in dev), which
    may not resolve from inside the cluster — rewrite to TP_API_URL.

    Signature for filesystem: redirect path starts with
    /api/terrapod/v1/storage/.
    """
    if not api_url:
        return location

    redir_host = urlparse(location).hostname
    api_host = urlparse(api_url).hostname
    if redir_host == api_host:
        return location

    parsed = urlparse(location)
    if _is_filesystem_storage_path(parsed.path):
        path_with_query = parsed.path
        if parsed.query:
            path_with_query += "?" + parsed.query
        return api_url.rstrip("/") + path_with_query

    return location


def _download_once(
    client: httpx.Client,
    url: str,
    output_path: Path,
    headers: dict[str, str],
    api_url: str,
) -> DownloadResult:
    """Single-shot core. One attempt of the redirect-aware download."""
    try:
        head_resp = client.get(url, headers=headers, follow_redirects=False)
    except httpx.RequestError as exc:
        logger.warning("download initial request failed", url=url, err=str(exc))
        return DownloadResult(ok=False, status=None)

    code = head_resp.status_code

    if code in (301, 302, 303, 307, 308):
        location = head_resp.headers.get("location", "")
        if not location:
            return DownloadResult(ok=False, status=code)
        rewritten = _maybe_rewrite_redirect(location, url, api_url)
        try:
            # Follow remaining hops automatically; surface body on
            # 4xx/5xx (S3 InvalidArgument XML, etc.) for diagnostics.
            with client.stream("GET", rewritten, follow_redirects=True) as stream:
                if not stream.is_success:
                    body = b""
                    try:
                        for chunk in stream.iter_bytes():
                            body += chunk
                            if len(body) >= 2048:
                                break
                    except httpx.RequestError:
                        pass
                    return DownloadResult(
                        ok=False,
                        status=stream.status_code,
                        body_preview=body[:2048].decode("utf-8", errors="replace"),
                    )
                with output_path.open("wb") as f:
                    for chunk in stream.iter_bytes():
                        f.write(chunk)
                return DownloadResult(ok=True, status=stream.status_code)
        except httpx.RequestError as exc:
            logger.warning("download redirect-follow failed", url=rewritten, err=str(exc))
            return DownloadResult(ok=False, status=None)

    if code == 200:
        # Direct 200 from the API itself (no redirect). The response
        # body is already in head_resp.content from the first request;
        # don't double-fetch. Sized artefacts (binary cache, state
        # tarballs) typically reach us via a presigned-URL 302 so this
        # path is only hit for tiny synchronous responses.
        output_path.write_bytes(head_resp.content)
        return DownloadResult(ok=True, status=200)

    # Any other status from the API hop is a hard fail.
    return DownloadResult(ok=False, status=code)


def download_to_file(
    url: str,
    output_path: Path,
    headers: dict[str, str] | None = None,
    *,
    api_url: str = "",
    retries: int = 3,
    retry_delay_seconds: float = 5.0,
    client: httpx.Client | None = None,
) -> DownloadResult:
    """Download `url` to `output_path` with the bash-compatible retry
    policy.

    Args:
        url: target URL. Auth header should be in `headers`.
        output_path: file to write the downloaded bytes to.
        headers: extra HTTP headers (typically `Authorization`).
        api_url: TP_API_URL — used only for redirect-hostname rewrite.
        retries: total attempts before giving up.
        retry_delay_seconds: sleep between retries.
        client: optional pre-built httpx.Client (for tests).

    Returns a DownloadResult; the caller decides whether to fail hard
    or fall through to a fallback download path.
    """
    headers = headers or {}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    own_client = False
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        own_client = True

    try:
        attempt = 1
        while True:
            result = _download_once(client, url, output_path, headers, api_url)
            if result.ok:
                return result

            # Same retry semantics as bash: retry only transient signals.
            transient = (
                result.status is None
                or result.status == 408
                or (result.status is not None and 500 <= result.status < 600)
            )
            if transient and attempt < retries:
                logger.info(
                    "download transient failure — will retry",
                    url=url,
                    status=result.status,
                    attempt=attempt,
                    of=retries,
                    delay_seconds=retry_delay_seconds,
                )
                time.sleep(retry_delay_seconds)
                attempt += 1
                # Clean partial output before the next attempt.
                output_path.unlink(missing_ok=True)
                continue

            return result
    finally:
        if own_client:
            client.close()
