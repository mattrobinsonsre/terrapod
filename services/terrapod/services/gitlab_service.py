"""GitLab VCS provider implementation.

Authenticates via Project/Group Access Token (stored encrypted on the
VCSConnection). Supports GitLab.com and self-hosted GitLab instances.
"""

from urllib.parse import quote as url_quote

import httpx

from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger
from terrapod.services.encryption_service import decrypt_value
from terrapod.services.vcs_provider import PullRequest

logger = get_logger(__name__)

DEFAULT_GITLAB_URL = "https://gitlab.com"


def _api_url(conn: VCSConnection) -> str:
    """Resolve the GitLab API base URL from the connection."""
    base = (conn.server_url or DEFAULT_GITLAB_URL).rstrip("/")
    return f"{base}/api/v4"


def _token(conn: VCSConnection) -> str:
    """Decrypt the stored access token."""
    if not conn.token_encrypted:
        raise ValueError("GitLab connection has no token configured")
    return decrypt_value(conn.token_encrypted)


def _headers(conn: VCSConnection) -> dict[str, str]:
    return {"PRIVATE-TOKEN": _token(conn)}


def _project_path(owner: str, repo: str) -> str:
    """URL-encode the project path for GitLab API."""
    return url_quote(f"{owner}/{repo}", safe="")


async def get_branch_sha(conn: VCSConnection, owner: str, repo: str, branch: str) -> str | None:
    """Get HEAD commit SHA for a branch."""
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/repository/branches/{url_quote(branch, safe='')}",
            headers=_headers(conn),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["commit"]["id"]


async def get_default_branch(conn: VCSConnection, owner: str, repo: str) -> str | None:
    """Get the repository's default branch name."""
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}",
            headers=_headers(conn),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("default_branch")


async def download_archive(conn: VCSConnection, owner: str, repo: str, ref: str) -> bytes:
    """Download repository tarball at a given ref."""
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            f"{api}/projects/{project}/repository/archive.tar.gz",
            params={"sha": ref},
            headers=_headers(conn),
        )
        resp.raise_for_status()
        return resp.content


async def list_open_prs(
    conn: VCSConnection, owner: str, repo: str, base_branch: str
) -> list[PullRequest]:
    """List open merge requests targeting the given base branch."""
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/merge_requests",
            params={
                "state": "opened",
                "target_branch": base_branch,
                "order_by": "updated_at",
                "sort": "desc",
                "per_page": 100,
            },
            headers=_headers(conn),
        )
        resp.raise_for_status()

    return [
        PullRequest(
            number=mr["iid"],
            head_sha=mr["sha"],
            head_ref=mr["source_branch"],
            title=mr["title"],
        )
        for mr in resp.json()
    ]


def parse_repo_url(repo_url: str) -> tuple[str, str] | None:
    """Parse a GitLab repo URL into (namespace, project).

    Supports:
      - https://gitlab.com/group/project
      - https://gitlab.com/group/subgroup/project
      - https://gitlab.example.com/group/project.git
      - git@gitlab.com:group/project.git

    For nested groups (group/subgroup/project), returns
    ("group/subgroup", "project").
    """
    url = repo_url.strip()

    # SSH format: git@gitlab.com:group/project.git
    if url.startswith("git@"):
        try:
            _, path = url.split(":", 1)
            path = path.removesuffix(".git")
            parts = path.rsplit("/", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
        except ValueError:
            pass
        return None

    # HTTPS format
    url = url.removesuffix(".git")
    if "://" in url:
        path = url.split("://", 1)[1]
        # Remove hostname
        _, _, remainder = path.partition("/")
        if remainder:
            parts = remainder.rsplit("/", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                return parts[0], parts[1]

    return None
