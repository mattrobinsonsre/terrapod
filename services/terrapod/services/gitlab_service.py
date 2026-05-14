"""GitLab VCS provider implementation.

Authenticates via Project/Group Access Token (stored on the VCSConnection).
Supports GitLab.com and self-hosted GitLab instances.
"""

import asyncio
from urllib.parse import quote as url_quote

import httpx

from terrapod.db.models import VCSConnection
from terrapod.logging_config import get_logger
from terrapod.services.vcs_provider import (
    MergeabilityStatus,
    PRComment,
    PRMergeResult,
    PRReview,
    PullRequest,
)

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
    """Download repository tarball at a given ref.

    Buffers the full tarball into process memory. See the github_service
    counterpart — use `download_archive_to_file` for the VCS-poll-cycle
    path to avoid OOMing the api pod on large monorepos.
    """
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


async def download_archive_to_file(
    conn: VCSConnection,
    owner: str,
    repo: str,
    ref: str,
    dest_path: str,
    *,
    chunk_size: int = 1 << 20,  # 1 MiB
    timeout: float = 300.0,
) -> int:
    """Stream a project tarball directly to a local file path.

    Avoids buffering the full archive in process memory — chunks land on
    disk as they arrive. Returns total bytes written.

    Retry policy: none. Same rationale as the github counterpart — the VCS
    poller reruns every `vcs.poll_interval_seconds`, so transport errors
    here are naturally retried by the next cycle. Mid-stream retries
    against the GitLab archive endpoint aren't resumable.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)
    url = f"{api}/projects/{project}/repository/archive.tar.gz"

    bytes_written = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream(
            "GET",
            url,
            params={"sha": ref},
            headers=_headers(conn),
        ) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    await asyncio.to_thread(f.write, chunk)
                    bytes_written += len(chunk)
    return bytes_written


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


async def list_branches(conn: VCSConnection, owner: str, repo: str) -> list[dict[str, str]]:
    """List repository branches.

    Returns a list of dicts with keys: name, sha.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/repository/branches",
            params={"per_page": 100},
            headers=_headers(conn),
        )
        resp.raise_for_status()

    return [{"name": b["name"], "sha": b["commit"]["id"]} for b in resp.json()]


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


async def list_repo_tree(conn: VCSConnection, owner: str, repo: str, ref: str) -> list[str] | None:
    """List every file path in the repo at `ref`.

    Used by the autodiscovery initial-scan path (#309). Paginates over
    `/projects/:id/repository/tree?recursive=true` until the server
    stops returning pages (or we hit a safety cap), collecting every
    `type=blob` entry.

    Returns None on a transport error so the caller can treat it the
    same as GitHub's `truncated` flag — best-effort, no scan today.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)
    files: list[str] = []
    page = 1
    # Hard cap so a misconfigured huge repo doesn't fan us out forever.
    # 200 pages × 100 per page = 20k files, plenty for realistic monorepos.
    MAX_PAGES = 200
    PER_PAGE = 100

    async with httpx.AsyncClient() as client:
        while page <= MAX_PAGES:
            try:
                resp = await client.get(
                    f"{api}/projects/{project}/repository/tree",
                    params={
                        "ref": ref,
                        "recursive": "true",
                        "per_page": PER_PAGE,
                        "page": page,
                    },
                    headers=_headers(conn),
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.warning(
                    "GitLab tree listing failed — autodiscovery initial scan will be incomplete",
                    project=f"{owner}/{repo}",
                    ref=ref,
                    page=page,
                    exc_info=True,
                )
                return None
            batch = resp.json()
            if not batch:
                return files
            for entry in batch:
                if entry.get("type") == "blob":
                    files.append(entry["path"])
            if len(batch) < PER_PAGE:
                return files
            page += 1

    logger.warning(
        "GitLab tree listing hit page cap — autodiscovery initial scan truncated",
        project=f"{owner}/{repo}",
        ref=ref,
    )
    return None


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


# ── Apply-then-merge surface (#282) ─────────────────────────────────────


# GitLab's `detailed_merge_status` values that mean "can merge". See
# https://docs.gitlab.com/api/merge_requests/#merge-status — values
# evolve across GitLab versions; we whitelist the safe ones and treat
# anything else as a block to surface verbatim.
_GITLAB_MERGEABLE_DETAILED = frozenset({"mergeable", "ci_must_pass", "ci_still_running"})


async def get_pull_request(
    conn: VCSConnection, owner: str, repo: str, mr_number: int
) -> PullRequest | None:
    """Fetch a single MR's current state."""
    api = _api_url(conn)
    project = _project_path(owner, repo)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/merge_requests/{mr_number}",
            headers=_headers(conn),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    mr = resp.json()
    return PullRequest(
        number=mr["iid"],
        head_sha=mr.get("sha") or "",
        head_ref=mr.get("source_branch") or "",
        title=mr.get("title") or "",
        draft=bool(mr.get("draft", False)),
        author_login=(mr.get("author") or {}).get("username", ""),
    )


async def get_pull_request_mergeability(
    conn: VCSConnection, owner: str, repo: str, mr_number: int
) -> MergeabilityStatus:
    """Apply-gate query.

    GitLab returns `merge_status` ("can_be_merged" / "cannot_be_merged" /
    "checking" / "unchecked") plus a `detailed_merge_status` (newer,
    finer-grained). We prefer the detailed status when present.
    `checking` / `unchecked` map to `unknown=True` so the caller retries.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/merge_requests/{mr_number}",
            headers=_headers(conn),
        )
        resp.raise_for_status()
    mr = resp.json()
    if mr.get("draft", False):
        return MergeabilityStatus(
            mergeable=False,
            state="draft",
            reason="Merge request is in draft state; mark as ready first.",
        )
    if mr.get("state") != "opened":
        return MergeabilityStatus(
            mergeable=False,
            state=mr.get("state", "unknown"),
            reason=f"Merge request is {mr.get('state')}.",
        )
    detailed = mr.get("detailed_merge_status") or ""
    legacy = mr.get("merge_status") or ""
    state = detailed or legacy
    if legacy in ("checking", "unchecked"):
        return MergeabilityStatus(
            mergeable=False,
            state=state,
            reason="GitLab is still computing mergeability; retry shortly.",
            unknown=True,
        )
    if detailed and detailed in _GITLAB_MERGEABLE_DETAILED:
        return MergeabilityStatus(mergeable=True, state=state, reason="")
    if legacy == "can_be_merged" and not detailed:
        return MergeabilityStatus(mergeable=True, state=state, reason="")
    # Anything else is a block — surface verbatim so the user sees
    # GitLab's own language (e.g. `discussions_not_resolved`,
    # `not_approved`, `conflict`).
    return MergeabilityStatus(
        mergeable=False,
        state=state,
        reason=f"GitLab reports merge status '{state}'.",
    )


async def merge_pull_request(
    conn: VCSConnection,
    owner: str,
    repo: str,
    mr_number: int,
    strategy: str,
    commit_title: str = "",
    commit_message: str = "",
) -> PRMergeResult:
    """Merge an MR via GitLab's merge API.

    Strategy mapping:
      - merge → default GitLab behaviour (merge commit, unless project
        is configured for fast-forward)
      - squash → `squash=true` (still produces a merge commit on top of
        the squashed commit unless project requires fast-forward)
      - rebase → fast-forward merge attempt. If project doesn't allow
        fast-forward, this falls back to the default and the caller
        sees the GitLab error verbatim.
    """
    if strategy not in ("merge", "squash", "rebase"):
        return PRMergeResult(merged=False, error_reason=f"invalid strategy {strategy!r}")
    api = _api_url(conn)
    project = _project_path(owner, repo)
    payload: dict = {}
    if strategy == "squash":
        payload["squash"] = True
        if commit_message:
            payload["squash_commit_message"] = commit_message
    if commit_title:
        payload["merge_commit_message"] = commit_title
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{api}/projects/{project}/merge_requests/{mr_number}/merge",
            json=payload,
            headers=_headers(conn),
        )
    if resp.status_code == 200:
        body = resp.json()
        return PRMergeResult(
            merged=body.get("state") == "merged",
            sha=body.get("merge_commit_sha") or body.get("squash_commit_sha") or "",
            message=body.get("merge_commit_message", ""),
        )
    try:
        msg = resp.json().get("message", resp.text)
    except Exception:
        msg = resp.text
    return PRMergeResult(merged=False, error_reason=f"{resp.status_code}: {msg}")


async def list_pr_comments_typed(
    conn: VCSConnection,
    owner: str,
    repo: str,
    mr_number: int,
    since: str | None = None,
) -> list[PRComment]:
    """List MR notes, typed and filtered for the comment-dispatch path.

    GitLab's notes API has no `since` parameter — we fetch with
    `sort=asc&order_by=updated_at` and filter client-side. System notes
    (state changes, assignment events) are filtered out — we only want
    human comments.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/merge_requests/{mr_number}/notes",
            params={"per_page": 100, "sort": "asc", "order_by": "updated_at"},
            headers=_headers(conn),
        )
        resp.raise_for_status()
    out: list[PRComment] = []
    for n in resp.json():
        if n.get("system"):
            continue
        if since and (n.get("updated_at") or "") <= since:
            continue
        author = n.get("author") or {}
        out.append(
            PRComment(
                id=str(n["id"]),
                body=n.get("body") or "",
                author_login=author.get("username", ""),
                author_user_id=str(author.get("id", "")),
                created_at=n.get("created_at", ""),
                updated_at=n.get("updated_at", ""),
            )
        )
    return out


async def list_pr_reviews(
    conn: VCSConnection, owner: str, repo: str, mr_number: int
) -> list[PRReview]:
    """Approvals on a GitLab MR, expressed as PRReview entries.

    GitLab approvals aren't event-like (no list of individual review
    submissions) — they're a snapshot. We synthesise one PRReview per
    current approver so the consumer can count them; `submitted_at` is
    left blank because the snapshot doesn't tell us when each approval
    landed.
    """
    api = _api_url(conn)
    project = _project_path(owner, repo)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{api}/projects/{project}/merge_requests/{mr_number}/approvals",
            headers=_headers(conn),
        )
        resp.raise_for_status()
    out: list[PRReview] = []
    for a in resp.json().get("approved_by", []):
        u = a.get("user") or {}
        out.append(
            PRReview(
                id=str(u.get("id", "")),
                state="approved",
                author_login=u.get("username", ""),
                submitted_at="",
            )
        )
    return out


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
