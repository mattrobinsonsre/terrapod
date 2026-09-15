"""What a module autodiscovery rule's `repo-url` names (#1620).

One string, three meanings, and the server decides which when the rule is
saved:

- **repository** — one repository (`owner/repo`, `group/sub/project`);
- **namespace** — every repository in an org, group or App installation's
  account (`owner`, `group`, `group/sub`);
- **pattern** — the repositories directly in one namespace whose name matches a
  glob (`owner/terraform-*`). Glob characters (`*`, `?`, `[]`) are allowed in
  the last segment only; neither provider allows them in a name, so they are
  unambiguous.

GitHub is decided by the string's shape — an account form must be the account
the connection's App is installed on — and GitLab by asking: a multi-segment
path is tried as a project first, which keeps every existing single-repository
rule exactly as it was, and then as a group. A GitLab user namespace is not
supported yet.

The decision is stored with the provider's id for the target and never flips
on its own: the poller works from the id, so a renamed or transferred target
keeps working, and only re-saving `repo-url` classifies it again. Silently
widening a single-repository rule to a whole group would be the dangerous
failure.

Errors carry an HTTP status: 422 for input that resolves to nothing, 502 when
the provider could not be asked, so that an apply can simply be retried.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from terrapod.db.models import VCSConnection
from terrapod.services.vcs_provider import RepositoryListing, RepositoryRef

KIND_REPOSITORY = "repository"
KIND_NAMESPACE = "namespace"
KIND_PATTERN = "pattern"
KINDS = (KIND_REPOSITORY, KIND_NAMESPACE, KIND_PATTERN)

GLOB_CHARS = frozenset("*?[]")
_GITHUB_WEB = "https://github.com"
_GITLAB_WEB = "https://gitlab.com"
_GIT_SUFFIX = re.compile(r"(\.git)?/*$", re.IGNORECASE)


class TargetError(Exception):
    """`repo-url` cannot be classified. `status` is the HTTP status to report."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class TargetGone(Exception):
    """The namespace a saved rule names no longer exists."""


@dataclass(frozen=True)
class Target:
    kind: str
    #: The provider's id: the repository's, or the namespace's.
    id: str
    #: The normalised path, e.g. `org/repo`, `group/sub` or `org/terraform-*`.
    path: str
    #: The namespace a pattern ranges over (or the repository's owner).
    namespace: str = ""
    #: The last-segment glob, for a pattern.
    glob: str = ""
    #: The web URL of the repository or namespace.
    url: str = ""
    #: The repository itself, for a repository target.
    ref: RepositoryRef | None = None


# ── Strings ──────────────────────────────────────────────────────────────


def web_base(conn: VCSConnection) -> str:
    """Where the connection's repositories live on the web.

    A GitHub connection's `server_url` is its API root — empty or
    `api.github.com` for github.com, `https://host/api/v3` for Enterprise,
    whose web root is the host. A GitLab connection's is the web root itself,
    relative URL root included.
    """
    raw = conn.server_url if isinstance(conn.server_url, str) else ""
    server = raw.strip().rstrip("/")
    if conn.provider == "gitlab":
        return server or _GITLAB_WEB
    parts = urlsplit(server)
    if not server or (parts.hostname or "").lower() == "api.github.com":
        return _GITHUB_WEB
    return f"{parts.scheme}://{parts.netloc}"


def canonical_url(conn: VCSConnection, path: str) -> str:
    return f"{web_base(conn)}/{path}"


def url_key(url: str) -> str:
    """A repository URL for matching: case, `.git` and trailing slashes aside.

    The same normalisation `module_autodiscovery_service` applies in SQL to a
    registered module's `vcs_repo_url`, so the two always agree.
    """
    return _GIT_SUFFIX.sub("", (url or "").strip().lower())


def is_full_url(value: str) -> bool:
    value = (value or "").strip()
    return "://" in value or value.startswith("git@")


def has_glob(value: str) -> bool:
    return any(c in GLOB_CHARS for c in value)


def normalise(conn: VCSConnection, raw: str) -> str:
    """`raw` as a path relative to the connection's web root.

    Accepts an https URL, `git@host:path`, or a bare path. Strips the scheme,
    `.git`, surrounding slashes, and the connection's web root (ignoring case,
    relative URL root included). A host other than the connection's is a 422.
    """
    value = (raw or "").strip()
    if not value:
        raise TargetError(422, "repo-url is required")
    base = urlsplit(web_base(conn))
    base_host = (base.hostname or "").lower()
    base_root = base.path.strip("/")
    if value.startswith("git@"):
        host, sep, path = value[4:].partition(":")
        if not sep:
            raise TargetError(422, f"cannot read repo-url {raw!r}")
    elif "://" in value:
        parts = urlsplit(value)
        host, path = parts.hostname or "", parts.path
    else:
        host, path = "", value
    if host and host.lower() != base_host:
        raise TargetError(
            422,
            f"repo-url is on {host!r}, but this connection is for {base_host!r}",
        )
    path = path.strip("/")
    if host and base_root and path.lower().startswith(base_root.lower() + "/"):
        path = path[len(base_root) + 1 :]
    path = _GIT_SUFFIX.sub("", path).strip("/")
    if not path or any(not segment for segment in path.split("/")):
        raise TargetError(422, f"repo-url {raw!r} does not name a repository or a namespace")
    return path


def split_glob(path: str) -> tuple[str, str]:
    """`(prefix, glob)` for a pattern, or `(path, "")` for anything else."""
    segments = path.split("/")
    if any(has_glob(s) for s in segments[:-1]):
        raise TargetError(
            422, "glob characters (*, ? and []) are allowed in the last segment of repo-url only"
        )
    if not has_glob(segments[-1]):
        return path, ""
    if len(segments) == 1:
        raise TargetError(
            422, "a pattern needs the namespace it ranges over, e.g. my-org/terraform-*"
        )
    return "/".join(segments[:-1]), segments[-1]


def owner_repo(conn: VCSConnection, repo_url: str) -> tuple[str, str] | None:
    """`(owner, repo)` for a repository URL — or path — on the connection."""
    from terrapod.services import github_service, gitlab_service

    parser = gitlab_service.parse_repo_url if conn.provider == "gitlab" else None
    if conn.provider == "github":
        parser = github_service.parse_repo_url
    parsed = parser(repo_url) if parser else None
    if parsed:
        return parsed
    try:
        path = normalise(conn, repo_url)
    except TargetError:
        return None
    if "/" not in path or has_glob(path):
        return None
    owner, _, repo = path.rpartition("/")
    return owner, repo


def matches_glob(ref: RepositoryRef, glob: str) -> bool:
    """Whether a repository's own name matches a pattern's glob, ignoring case."""
    return fnmatch.fnmatchcase(ref.name.lower(), glob.lower())


# ── Classification ───────────────────────────────────────────────────────


async def classify(conn: VCSConnection, raw: str) -> Target:
    """Decide what `raw` names on `conn`, asking the provider as needed."""
    path = normalise(conn, raw)
    prefix, glob = split_glob(path)
    try:
        if conn.provider == "github":
            return await _classify_github(conn, path, glob)
        if conn.provider == "gitlab":
            return await _classify_gitlab(conn, path, prefix, glob)
    except TargetError:
        raise
    except httpx.HTTPStatusError as exc:
        raise TargetError(
            502,
            f"the VCS provider answered HTTP {exc.response.status_code} while checking "
            f"{path!r}; try again",
        ) from exc
    except Exception as exc:
        raise TargetError(
            502, f"could not ask the VCS provider about {path!r}: {exc}; try again"
        ) from exc
    raise TargetError(422, f"unknown VCS provider: {conn.provider!r}")


async def _classify_github(conn: VCSConnection, path: str, glob: str) -> Target:
    from terrapod.services import github_service

    segments = path.split("/")
    if len(segments) > 2:
        raise TargetError(
            422,
            "a GitHub repo-url names an account, account/repository or account/pattern, "
            f"not {path!r}",
        )
    if len(segments) == 2 and not glob:
        data = await github_service.get_repository(conn, segments[0], segments[1])
        if data is None:
            raise TargetError(
                422,
                f"repository {path!r} was not found, or this connection's GitHub App cannot see it",
            )
        ref = github_service.repository_ref(data)
        return Target(KIND_REPOSITORY, ref.id, ref.path, ref.owner, "", ref.url, ref)

    account = await _github_account(conn, segments[0])
    login = account.get("login") or segments[0]
    return Target(
        KIND_PATTERN if glob else KIND_NAMESPACE,
        str(account.get("id") or ""),
        f"{login}/{glob}" if glob else login,
        login,
        glob,
        canonical_url(conn, login),
    )


async def _github_account(conn: VCSConnection, owner: str) -> dict:
    """The account the connection's App is installed on, if `owner` names it.

    An App installation lists only its own account's repositories, so a
    namespace or pattern rule can only ever range over that one account.
    """
    from terrapod.services import github_service

    login = conn.github_account_login
    expected = login.strip() if isinstance(login, str) else ""
    if not expected:
        listing = await github_service.list_installation_repositories(conn, max_repositories=1)
        expected = listing.repositories[0].owner if listing.repositories else ""
    if not expected:
        raise TargetError(
            422,
            "cannot tell which account this connection's GitHub App is installed on; set "
            "the connection's github-account-login",
        )
    if owner.lower() != expected.lower():
        raise TargetError(
            422,
            f"this connection's GitHub App is installed on {expected!r}, not {owner!r}: "
            "a repo-url that names an account must name that one",
        )
    account = await github_service.get_account(conn, expected)
    if account is None:
        raise TargetError(422, f"GitHub account {expected!r} was not found")
    return account


async def _classify_gitlab(conn: VCSConnection, path: str, prefix: str, glob: str) -> Target:
    from terrapod.services import gitlab_service

    if glob:
        group = await gitlab_service.get_group(conn, prefix)
        if group is None:
            await _gitlab_not_found(conn, prefix)
        namespace = group.get("full_path") or prefix
        return Target(
            KIND_PATTERN,
            str(group.get("id") or ""),
            f"{namespace}/{glob}",
            namespace,
            glob,
            group.get("web_url") or canonical_url(conn, namespace),
        )
    # A project first: every rule saved before #1620 names one.
    if "/" in path:
        project = await gitlab_service.get_project(conn, path)
        if project is not None:
            ref = gitlab_service.project_ref(project)
            return Target(KIND_REPOSITORY, ref.id, ref.path, ref.owner, "", ref.url, ref)
    group = await gitlab_service.get_group(conn, path)
    if group is None:
        await _gitlab_not_found(conn, path)
    namespace = group.get("full_path") or path
    return Target(
        KIND_NAMESPACE,
        str(group.get("id") or ""),
        namespace,
        namespace,
        "",
        group.get("web_url") or canonical_url(conn, namespace),
    )


async def _gitlab_not_found(conn: VCSConnection, path: str) -> None:
    from terrapod.services import gitlab_service

    namespace = await gitlab_service.get_namespace(conn, path)
    if namespace is not None and namespace.get("kind") == "user":
        raise TargetError(
            422,
            f"{path!r} is a GitLab user namespace, which a rule cannot range over yet; "
            "name a group, a project, or a pattern in a group",
        )
    raise TargetError(422, f"no GitLab project or group {path!r} is visible to this connection")


# ── The poller's view: by id ─────────────────────────────────────────────


async def repository_by_id(conn: VCSConnection, repo_id: str) -> RepositoryRef | None:
    """A repository by its provider id, wherever it now lives; None if gone."""
    from terrapod.services import github_service, gitlab_service

    if conn.provider == "gitlab":
        data = await gitlab_service.get_project(conn, repo_id)
        return gitlab_service.project_ref(data) if data else None
    data = await github_service.get_repository_by_id(conn, repo_id)
    return github_service.repository_ref(data) if data else None


async def list_repositories(
    conn: VCSConnection, kind: str, target_id: str, glob: str, *, max_repositories: int
) -> RepositoryListing:
    """The repositories a namespace or pattern rule ranges over, by path.

    Works from the target's id. A GitHub rule's account is the App
    installation's, so its listing is the installation's, kept to that
    account; a GitLab rule lists its group — subgroups included for a
    namespace, direct children only for a pattern. Raises `TargetGone` when a
    GitLab group no longer exists, and the provider's error on a failure.
    """
    from terrapod.services import github_service, gitlab_service

    if conn.provider == "github":
        listing = await github_service.list_installation_repositories(
            conn, max_repositories=max_repositories
        )
        refs = [r for r in listing.repositories if r.owner_id == target_id]
    elif conn.provider == "gitlab":
        found = await gitlab_service.list_group_projects(
            conn,
            target_id,
            include_subgroups=kind == KIND_NAMESPACE,
            max_repositories=max_repositories,
        )
        if found is None:
            raise TargetGone(f"the group this rule names (id {target_id}) no longer exists")
        listing = found
        refs = list(listing.repositories)
        if kind == KIND_PATTERN:
            refs = [r for r in refs if not r.owner_id or r.owner_id == target_id]
    else:
        raise ValueError(f"unknown VCS provider: {conn.provider!r}")
    if glob:
        refs = [r for r in refs if matches_glob(r, glob)]
    return RepositoryListing(sorted(refs, key=lambda r: r.path.lower()), listing.complete)
