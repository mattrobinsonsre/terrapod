"""GitHub App service for VCS integration.

Handles JWT generation, installation token management, and repository
operations via the GitHub REST API. All credentials (app_id, private key)
are stored on the VCSConnection.
"""

import asyncio
import hashlib
import hmac
import time

import httpx
import jwt

from terrapod.config import settings
from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_GITHUB_API_URL = "https://api.github.com"

# Hard cap on how long we'll wait between retries. GitHub's X-RateLimit-Reset
# on the primary rate limit can be an hour out; we don't want to tie up a
# poll-cycle coroutine that long — a short wait is enough to ride out a burst,
# and another poll cycle will come along soon enough if we give up.
_MAX_RETRY_WAIT_SECONDS = 60.0
_DEFAULT_BACKOFF_SECONDS = 5.0
_MAX_RETRIES = 3
# Only these methods get retried on 5xx. POST/PATCH/PUT/DELETE may have
# already executed server-side when the 5xx was returned (e.g. the PR
# comment was written but the response was lost), so retrying them risks
# duplicate side effects. All methods still retry on 429 / rate-limit
# 403 — those are pre-execution rejections with no side effect.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Upper bound on how many bytes of a response body we'll decode to look
# for GitHub's "secondary rate limit" marker. Large-body responses (like
# archive downloads returning 403 because of lost access) should not be
# fully decoded just to substring-search.
_BODY_SCAN_LIMIT_BYTES = 4096


def _parse_retry_delay(resp: httpx.Response) -> float:
    """Compute how long to wait before retrying a rate-limited GitHub response.

    GitHub sets `Retry-After` on 429 responses and on some 403s (secondary
    rate limit). Primary rate-limit 403s don't set Retry-After but do set
    `X-RateLimit-Reset` (an epoch seconds value). We prefer Retry-After,
    fall back to X-RateLimit-Reset, and finally to a fixed backoff. All
    values are clamped to _MAX_RETRY_WAIT_SECONDS.
    """
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return max(1.0, min(float(retry_after), _MAX_RETRY_WAIT_SECONDS))
        except ValueError:
            pass  # HTTP-date form — rare; fall through to next strategy
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            wait = float(reset) - time.time()
            return max(1.0, min(wait, _MAX_RETRY_WAIT_SECONDS))
        except ValueError:
            pass
    return _DEFAULT_BACKOFF_SECONDS


def _looks_like_secondary_rate_limit(resp: httpx.Response) -> bool:
    """Check a 403 body for the 'secondary rate limit' marker, safely.

    Only inspects the first few KiB and only when the content-type is
    JSON/text — anything else (e.g. a tarball 403'd for lost access)
    is returned as-is without decoding.
    """
    content_type = resp.headers.get("Content-Type", "").lower()
    if not ("json" in content_type or content_type.startswith("text/")):
        return False
    # resp.content is the raw bytes buffer; slicing avoids decoding
    # potentially huge bodies just to find an ASCII marker.
    body = resp.content[:_BODY_SCAN_LIMIT_BYTES]
    try:
        sample = body.decode("utf-8", errors="replace").lower()
    except Exception:
        return False
    return "secondary rate limit" in sample


def _should_retry(resp: httpx.Response, method: str, retry_5xx: bool) -> bool:
    """Return True if the response status deserves a retry.

    - 429 and rate-limit 403: always retry (pre-execution rejection, no
      side effect, so safe to replay on any method).
    - 5xx: retry only when ``retry_5xx`` is True. By default this is the
      "method is idempotent" check (GET/HEAD/OPTIONS) — a POST that
      returned 502 may have already been applied server-side and
      replaying it could duplicate the operation (e.g. an extra PR
      comment). Callers with endpoints that ARE safe to replay
      regardless of HTTP method (e.g. GitHub's installation-token
      acquisition — the server-side effect is only "issue a new token",
      which never conflicts) pass ``retry_5xx=True``.
    """
    if resp.status_code == 429:
        return True
    if resp.status_code == 403 and (
        resp.headers.get("X-RateLimit-Remaining") == "0" or _looks_like_secondary_rate_limit(resp)
    ):
        return True
    if 500 <= resp.status_code < 600:
        return retry_5xx or method.upper() in _IDEMPOTENT_METHODS
    return False


async def _github_request(
    method: str,
    url: str,
    token: str,
    *,
    follow_redirects: bool = False,
    retry_5xx: bool = False,
    **kwargs: object,
) -> httpx.Response:
    """Authenticated GitHub API request with retry on 429 / secondary-rate-limit / 5xx.

    Standard headers (Authorization, Accept, API version) are added automatically.
    The returned response is NOT raised-for-status — callers may still want to
    inspect specific statuses (e.g. 404).

    Retry scope:
    - 429 and rate-limit 403: every method, every attempt.
    - 5xx: only on idempotent methods (GET/HEAD/OPTIONS) by default.
      Pass ``retry_5xx=True`` for endpoints that are safe to replay
      regardless of method (e.g. token issuance).
    - Transport errors (connect / read / timeout): every method — they
      happen before the server has a chance to process the request, so
      replay is safe.
    """
    headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})  # type: ignore[arg-type]
    headers.setdefault("Authorization", f"Bearer {token}")
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("X-GitHub-Api-Version", "2022-11-28")

    async with httpx.AsyncClient(follow_redirects=follow_redirects) as client:
        resp: httpx.Response | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
            except httpx.TransportError as e:
                # Connect errors / read timeouts / protocol errors — the
                # request didn't land, so it's safe to retry any method.
                if attempt >= _MAX_RETRIES:
                    logger.warning(
                        "GitHub transport error, retries exhausted",
                        method=method,
                        url=url,
                        error=str(e),
                    )
                    raise
                logger.warning(
                    "GitHub transport error, retrying",
                    method=method,
                    url=url,
                    error=str(e),
                    attempt=attempt + 1,
                )
                await asyncio.sleep(_DEFAULT_BACKOFF_SECONDS)
                continue

            if not _should_retry(resp, method, retry_5xx):
                return resp
            if attempt >= _MAX_RETRIES:
                logger.warning(
                    "GitHub retries exhausted, returning last response",
                    method=method,
                    url=url,
                    status=resp.status_code,
                )
                return resp
            delay = _parse_retry_delay(resp)
            logger.warning(
                "GitHub request rate-limited or failed, retrying",
                method=method,
                url=url,
                status=resp.status_code,
                delay_seconds=delay,
                attempt=attempt + 1,
            )
            await asyncio.sleep(delay)
        # Unreachable: loop either returns or exhausts and returns.
        assert resp is not None
        return resp


# Installation token cache: {installation_id: (token, expires_at_epoch)}
_token_cache: dict[int, tuple[str, float]] = {}


def _api_url(conn: VCSConnection) -> str:
    """Resolve the GitHub API URL from the connection."""
    return (conn.server_url or DEFAULT_GITHUB_API_URL).rstrip("/")


def _private_key(conn: VCSConnection) -> str:
    """Get the GitHub App private key from the connection."""
    if not conn.token:
        raise ValueError("GitHub connection has no private key configured")
    return conn.token


def _generate_app_jwt(app_id: int, private_key: str) -> str:
    """Generate a short-lived JWT for GitHub App authentication.

    The JWT is signed with RS256 using the app's private key and has a
    10-minute lifetime (GitHub maximum).
    """
    now = int(time.time())
    payload = {
        "iat": now - 60,  # 60s clock skew allowance
        "exp": now + (10 * 60),  # 10 minutes
        "iss": str(app_id),
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(conn: VCSConnection) -> str:
    """Get an installation access token, using a 50-minute cache.

    Installation tokens are valid for 1 hour. We cache for 50 minutes
    to ensure we never use an expired token.
    """
    installation_id = conn.github_installation_id
    cached = _token_cache.get(installation_id)
    if cached:
        token, expires_at = cached
        if time.time() < expires_at:
            return token

    app_jwt = _generate_app_jwt(conn.github_app_id, _private_key(conn))
    api_url = _api_url(conn)

    # This call predates the caller having a token, so it uses the JWT
    # directly as the bearer. The token endpoint is semantically idempotent
    # (every call mints a fresh short-lived token; replaying after a 5xx
    # is safe), so opt in to 5xx retries via retry_5xx=True — otherwise a
    # single transient 5xx would propagate as an auth failure and stall
    # every subsequent VCS operation on this connection.
    resp = await _github_request(
        "POST",
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        app_jwt,
        retry_5xx=True,
    )
    resp.raise_for_status()
    data = resp.json()

    token = data["token"]
    # Cache for 50 minutes (tokens last 60 min)
    _token_cache[installation_id] = (token, time.time() + 50 * 60)

    logger.debug("GitHub installation token obtained", installation_id=installation_id)
    return token


def validate_webhook_signature(payload: bytes, signature_header: str) -> bool:
    """Validate GitHub webhook HMAC-SHA256 signature.

    Only used when webhooks are enabled (webhook_secret is configured).
    """
    secret = settings.vcs.github.webhook_secret
    if not secret:
        return False

    if not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")

    return hmac.compare_digest(expected, received)


async def get_repo_branch_sha(
    conn: VCSConnection, owner: str, repo: str, branch: str
) -> str | None:
    """Get the HEAD commit SHA for a branch.

    Returns None if the branch doesn't exist or isn't accessible.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request("GET", f"{api_url}/repos/{owner}/{repo}/branches/{branch}", token)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["commit"]["sha"]


async def get_repo_default_branch(conn: VCSConnection, owner: str, repo: str) -> str | None:
    """Get the default branch name for a repository.

    Returns None if the repo doesn't exist or isn't accessible.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request("GET", f"{api_url}/repos/{owner}/{repo}", token)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["default_branch"]


async def download_repo_archive(conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
    """Download a repository tarball for a given ref (branch, tag, or SHA).

    Uses GitHub's tarball endpoint which returns a redirect to a CDN URL.

    Loads the full tarball into process memory. Safe for small registry
    archives but DANGEROUS for full monorepo poll-cycle archives — the api
    pod will OOM under enough concurrent workspace polls. Use
    `download_repo_archive_to_file` for the VCS-poll path.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "GET",
        f"{api_url}/repos/{owner}/{repo}/tarball/{ref}",
        token,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.content


async def download_repo_archive_to_file(
    conn: VCSConnection,
    owner: str,
    repo: str,
    ref: str,
    dest_path: str,
    *,
    chunk_size: int = 1 << 20,  # 1 MiB
    timeout: float = 300.0,
) -> int:
    """Stream a repository tarball directly to a local file path.

    Avoids buffering the full archive in process memory — chunks land on
    disk as they arrive over the wire, so a 500 MB tarball uses ~chunk_size
    of RAM regardless of repo size. Returns the total bytes written.

    Retry policy: none. Transport errors propagate to the caller. The VCS
    poller reruns every `vcs.poll_interval_seconds` (default 60s), so a
    transient network hiccup is naturally retried by the next cycle. We
    deliberately don't retry inline because mid-stream retries are unsafe
    (no resumable offset against the GitHub tarball endpoint) and pre-byte
    retries would just duplicate the cycle's own retry cadence.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)
    url = f"{api_url}/repos/{owner}/{repo}/tarball/{ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    bytes_written = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    # Disk write is sync; offload to a thread so we don't
                    # block the event loop on every chunk.
                    await asyncio.to_thread(f.write, chunk)
                    bytes_written += len(chunk)
    return bytes_written


async def list_open_pull_requests(
    conn: VCSConnection, owner: str, repo: str, base_branch: str
) -> list[dict]:
    """List open pull requests targeting a specific base branch.

    Returns a list of dicts with keys: number, head_sha, head_ref, title.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "GET",
        f"{api_url}/repos/{owner}/{repo}/pulls",
        token,
        params={
            "state": "open",
            "base": base_branch,
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
        },
    )
    resp.raise_for_status()

    return [
        {
            "number": pr["number"],
            "head_sha": pr["head"]["sha"],
            "head_ref": pr["head"]["ref"],
            "title": pr["title"],
        }
        for pr in resp.json()
    ]


async def list_repo_branches(conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
    """List repository branches.

    Returns a list of dicts with keys: name, sha.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "GET",
        f"{api_url}/repos/{owner}/{repo}/branches",
        token,
        params={"per_page": 100},
    )
    resp.raise_for_status()

    return [{"name": b["name"], "sha": b["commit"]["sha"]} for b in resp.json()]


async def list_repo_tags(conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
    """List repository tags.

    Returns a list of dicts with keys: name, sha.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "GET",
        f"{api_url}/repos/{owner}/{repo}/tags",
        token,
        params={"per_page": 100},
    )
    resp.raise_for_status()

    return [{"name": tag["name"], "sha": tag["commit"]["sha"]} for tag in resp.json()]


async def get_changed_files(
    conn: VCSConnection, owner: str, repo: str, base_sha: str, head_sha: str
) -> list[str] | None:
    """Get list of file paths changed between two commits.

    Uses the compare endpoint: GET /repos/{owner}/{repo}/compare/{base}...{head}
    Returns None if the response is truncated (GitHub caps at 300 files),
    signaling that the caller should not filter and should create the run.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "GET",
        f"{api_url}/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        token,
    )
    resp.raise_for_status()

    data = resp.json()
    files = data.get("files", [])
    if len(files) >= 300:
        logger.warning(
            "GitHub compare truncated (300+ files), skipping subdirectory filter",
            owner=owner,
            repo=repo,
        )
        return None
    return [f["filename"] for f in files]


async def create_commit_status(
    conn: VCSConnection,
    owner: str,
    repo: str,
    sha: str,
    state: str,
    description: str,
    target_url: str = "",
    context: str = "terrapod",
) -> None:
    """Post a commit status to GitHub.

    Args:
        state: One of pending, success, failure, error.
        description: Max 140 chars.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    body: dict[str, str] = {
        "state": state,
        "description": description[:140],
        "context": context,
    }
    if target_url:
        body["target_url"] = target_url

    # Commit status is idempotent in practice — GitHub keeps only the
    # latest status per (sha, context), so replaying the same body just
    # re-asserts what we meant. Opt in to 5xx retry so a transient 502
    # doesn't leave a PR check stuck on an older state.
    resp = await _github_request(
        "POST",
        f"{api_url}/repos/{owner}/{repo}/statuses/{sha}",
        token,
        json=body,
        retry_5xx=True,
    )
    resp.raise_for_status()

    logger.debug(
        "GitHub commit status posted",
        owner=owner,
        repo=repo,
        sha=sha[:8],
        state=state,
    )


async def create_pr_comment(
    conn: VCSConnection, owner: str, repo: str, pr_number: int, body: str
) -> int:
    """Create a comment on a PR. Returns the comment ID."""
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "POST",
        f"{api_url}/repos/{owner}/{repo}/issues/{pr_number}/comments",
        token,
        json={"body": body},
    )
    resp.raise_for_status()
    return resp.json()["id"]


async def update_pr_comment(
    conn: VCSConnection, owner: str, repo: str, comment_id: int, body: str
) -> None:
    """Update an existing PR comment."""
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "PATCH",
        f"{api_url}/repos/{owner}/{repo}/issues/comments/{comment_id}",
        token,
        json={"body": body},
    )
    resp.raise_for_status()


async def list_pr_comments(
    conn: VCSConnection, owner: str, repo: str, pr_number: int
) -> list[dict]:
    """List comments on a PR. Used for marker-based comment lookup."""
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    resp = await _github_request(
        "GET",
        f"{api_url}/repos/{owner}/{repo}/issues/{pr_number}/comments",
        token,
        params={"per_page": 100},
    )
    resp.raise_for_status()
    return resp.json()


def parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    """Parse a GitHub repo URL into (owner, repo).

    Supports:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git

    Returns None if the URL can't be parsed.
    """
    url = repo_url.strip()

    # SSH format: git@github.com:owner/repo.git
    if url.startswith("git@"):
        try:
            _, path = url.split(":", 1)
            path = path.removesuffix(".git")
            parts = path.split("/")
            if len(parts) == 2:
                return parts[0], parts[1]
        except ValueError:
            pass
        return None

    # HTTPS format: https://github.com/owner/repo[.git]
    url = url.removesuffix(".git")
    # Strip protocol and host
    for prefix in ("https://github.com/", "http://github.com/"):
        if url.startswith(prefix):
            path = url.removeprefix(prefix)
            parts = path.split("/")
            if len(parts) >= 2:
                return parts[0], parts[1]
            return None

    # GitHub Enterprise: strip host and take first two path segments
    if "://" in url:
        path = url.split("://", 1)[1]
        # Remove hostname
        parts = path.split("/", 1)
        if len(parts) == 2:
            remaining = parts[1].split("/")
            if len(remaining) >= 2:
                return remaining[0], remaining[1]

    return None
