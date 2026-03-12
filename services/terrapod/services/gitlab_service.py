"""GitLab VCS provider implementation.

Authenticates via Project/Group Access Token (stored on the VCSConnection).
Supports GitLab.com and self-hosted GitLab instances.
"""

from urllib.parse import quote as url_quote

import httpx

from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger
from terrapod.services.vcs_provider import PullRequest

logger = get_logger(__name__)

DEFAULT_GITLAB_URL = "https://gitlab.com"


def _api_url(conn: VCSConnection) -> str:
    """Resolve the GitLab API base URL from the connection."""
    base = (conn.server_url or DEFAULT_GITLAB_URL).rstrip("/")
    return f"{base}/api/v4"


def _token(conn: VCSConnection) -> str:
    """Get the stored access token."""
    if not conn.token:
        raise ValueError("GitLab connection has no token configured")
    return conn.token


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


async def list_tags(conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
    """List repository tags.

    Returns a list of dicts with keys: name, sha.
    """
    api = _api_url(conn)
    tok = _token(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/repository/tags",
            params={"per_page": 100},
            headers={"PRIVATE-TOKEN": tok},
        )
        resp.raise_for_status()

    return [{"name": tag["name"], "sha": tag["commit"]["id"]} for tag in resp.json()]


async def get_changed_files(
    conn: VCSConnection, owner: str, repo: str, base_sha: str, head_sha: str
) -> list[str] | None:
    """Get list of file paths changed between two commits.

    Uses the compare endpoint: GET /projects/{id}/repository/compare
    Collects both old_path and new_path from diffs to catch renames.
    Returns None if the response may be truncated (GitLab defaults vary),
    signaling that the caller should not filter and should create the run.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/repository/compare",
            params={"from": base_sha, "to": head_sha, "per_page": 500},
            headers=_headers(conn),
        )
        resp.raise_for_status()

    data = resp.json()
    diffs = data.get("diffs", [])
    if len(diffs) >= 500:
        logger.warning(
            "GitLab compare may be truncated (500+ diffs), skipping subdirectory filter",
            project=f"{owner}/{repo}",
        )
        return None

    files: set[str] = set()
    for diff in diffs:
        files.add(diff["new_path"])
        files.add(diff["old_path"])
    return list(files)


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
    """Post a commit status to GitLab.

    Args:
        state: One of pending, running, success, failed, canceled.
        description: Status description text.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)

    params: dict[str, str] = {
        "state": state,
        "description": description[:140],
        "name": context,
    }
    if target_url:
        params["target_url"] = target_url

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{api}/projects/{project}/statuses/{sha}",
            json=params,
            headers=_headers(conn),
        )
        resp.raise_for_status()

    logger.debug(
        "GitLab commit status posted",
        project=f"{owner}/{repo}",
        sha=sha[:8],
        state=state,
    )


async def create_mr_comment(
    conn: VCSConnection, owner: str, repo: str, mr_number: int, body: str
) -> int:
    """Create a note on a merge request. Returns the note ID."""
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{api}/projects/{project}/merge_requests/{mr_number}/notes",
            json={"body": body},
            headers=_headers(conn),
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def update_mr_comment(
    conn: VCSConnection, owner: str, repo: str, mr_number: int, note_id: int, body: str
) -> None:
    """Update an existing merge request note."""
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{api}/projects/{project}/merge_requests/{mr_number}/notes/{note_id}",
            json={"body": body},
            headers=_headers(conn),
        )
        resp.raise_for_status()


async def list_mr_comments(
    conn: VCSConnection, owner: str, repo: str, mr_number: int
) -> list[dict]:
    """List notes on a merge request. Used for marker-based comment lookup."""
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/merge_requests/{mr_number}/notes",
            params={"per_page": 100, "sort": "desc"},
            headers=_headers(conn),
        )
        resp.raise_for_status()
        return resp.json()


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
