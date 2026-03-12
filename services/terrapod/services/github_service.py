"""GitHub App service for VCS integration.

Handles JWT generation, installation token management, and repository
operations via the GitHub REST API. All credentials (app_id, private key)
are stored on the VCSConnection.
"""

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

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{api_url}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api_url}/repos/{owner}/{repo}/branches/{branch}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
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

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api_url}/repos/{owner}/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["default_branch"]


async def download_repo_archive(conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
    """Download a repository tarball for a given ref (branch, tag, or SHA).

    Uses GitHub's tarball endpoint which returns a redirect to a CDN URL.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            f"{api_url}/repos/{owner}/{repo}/tarball/{ref}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        return resp.content


async def list_open_pull_requests(
    conn: VCSConnection, owner: str, repo: str, base_branch: str
) -> list[dict]:
    """List open pull requests targeting a specific base branch.

    Returns a list of dicts with keys: number, head_sha, head_ref, title.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api_url}/repos/{owner}/{repo}/pulls",
            params={
                "state": "open",
                "base": base_branch,
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
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


async def list_repo_tags(conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
    """List repository tags.

    Returns a list of dicts with keys: name, sha.
    """
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api_url}/repos/{owner}/{repo}/tags",
            params={"per_page": 100},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api_url}/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{api_url}/repos/{owner}/{repo}/statuses/{sha}",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{api_url}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def update_pr_comment(
    conn: VCSConnection, owner: str, repo: str, comment_id: int, body: str
) -> None:
    """Update an existing PR comment."""
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{api_url}/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()


async def list_pr_comments(
    conn: VCSConnection, owner: str, repo: str, pr_number: int
) -> list[dict]:
    """List comments on a PR. Used for marker-based comment lookup."""
    token = await get_installation_token(conn)
    api_url = _api_url(conn)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api_url}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            params={"per_page": 100},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
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
