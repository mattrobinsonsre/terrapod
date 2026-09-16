"""Module autodiscovery (#1584, #1620): rules that find modules in repositories
and register them in the private registry.

The module registry's counterpart to workspace autodiscovery. A rule names a
VCS connection and a `repo-url` — one repository, an org or group, or a pattern
over one namespace's repositories (see `module_autodiscovery_targets`) — a glob
`pattern` (minus `ignore_patterns`) over each repository's Terraform files, and
how each module is named. Every matching file's directory is a candidate — a
repository root or a submodule (#1583):

- **preview** proposes the candidates and registers nothing;
- **scan** registers them, all or a chosen subset, through the ordinary
  module-create fields, each with its `subdirectory`;
- **poll** (from the registry VCS poll cycle) registers new candidates when a
  tracked branch moves, so a submodule added to a repository — or, for an
  org-wide rule, a repository created after the rule's baseline — is picked up
  without anyone touching the registry.

Scan state is kept per repository, in `ModuleAutodiscoveryRepository`. A
repository that already existed when the rule first saw it is a **baseline**:
its first scan records what is there and registers nothing, which an operator
registers with an explicit scan; after that, directories new to it register
automatically. A repository **created** after the rule's baseline registers
everything it holds.

Nothing here ever deletes or renames a module: a directory that disappears
simply stops producing versions, because tags without it are already skipped,
and a repository that leaves a rule's scope keeps the modules it produced.

Patterns are the same gitignore-style globs as workspace rules, matched against
file paths. On top of them, directories that conventionally hold something other
than a module to publish (examples, tests, fixtures, hidden directories) are
never candidates, and only `.tf`/`.tf.json` files count — a directory of
`.tfvars` is a root configuration, not a module.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.db.models import (
    ModuleAutodiscoveryRepository,
    ModuleAutodiscoveryRule,
    RegistryModule,
    VCSConnection,
)
from terrapod.logging_config import get_logger
from terrapod.services import module_autodiscovery_targets as targets
from terrapod.services import vcs_rate_limit
from terrapod.services.label_validation import sanitize_labels
from terrapod.services.module_discovery import (
    candidate_directories,
    fit_name,
    module_base_name,
    repo_name_from_url,
    suggest_name,
    suggest_provider,
)
from terrapod.services.vcs_provider import RepositoryListing, RepositoryRef
from terrapod.services.workspace_autodiscovery_service import _is_ignored, _match_glob

logger = get_logger(__name__)

_MODULE_FILE_SUFFIXES = (".tf", ".tf.json")

#: A name-template placeholder. The only ones there are.
TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{(repo|path|leaf|root|owner)\}")
#: A whole valid name-template: literal text (no braces) and the placeholders.
TEMPLATE_RE = re.compile(r"^(?:[^{}]|\{(?:repo|path|leaf|root|owner)\})*$")

#: Statuses of a repository whose candidates a rule may register.
SCANNABLE_STATUSES = frozenset({"active", "empty", "no-branch", "error"})

_BACKOFF_BASE_SECONDS = 300
_BACKOFF_MAX_SECONDS = 6 * 3600


# ── Repositories as the registry sees them ───────────────────────────────


@dataclass(frozen=True)
class RepoContext:
    """A repository modules are registered from.

    `url` is what those modules record as their `vcs_repo_url`; any
    `previous_urls` (a repository renamed since) still count when deciding
    what is already registered.
    """

    url: str
    path: str
    previous_urls: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def owner(self) -> str:
        return self.path.rpartition("/")[0]

    @property
    def key(self) -> str:
        return targets.url_key(self.url)

    @property
    def urls(self) -> tuple[str, ...]:
        return (self.url, *self.previous_urls)


# ── Candidates ───────────────────────────────────────────────────────────


def _claims(path: str, pattern: str, ignore_patterns: list[str]) -> bool:
    if not path.endswith(_MODULE_FILE_SUFFIXES):
        return False
    if _is_ignored(path, ignore_patterns):
        return False
    return _match_glob(path, pattern)


def _candidates(pattern: str, ignore_patterns: list[str], file_paths: list[str]) -> list[str]:
    """Pure, and free of ORM objects, so it can run on a worker thread."""
    return candidate_directories([p for p in file_paths if _claims(p, pattern, ignore_patterns)])


def rule_claims_path(rule: ModuleAutodiscoveryRule, path: str) -> bool:
    """Whether the rule counts the file at `path` towards a module. Pure."""
    return _claims(path, rule.pattern, rule.ignore_patterns or [])


def candidate_subdirectories(rule: ModuleAutodiscoveryRule, file_paths: list[str]) -> list[str]:
    """The module directories the rule claims, the repository root first."""
    return _candidates(rule.pattern, list(rule.ignore_patterns or []), file_paths)


async def candidates_off_loop(rule: ModuleAutodiscoveryRule, file_paths: list[str]) -> list[str]:
    """`candidate_subdirectories` on a worker thread: a large tree is tens of
    thousands of paths, each matched against globs, and that must not stall
    the event loop."""
    return await asyncio.to_thread(
        _candidates, rule.pattern, list(rule.ignore_patterns or []), file_paths
    )


def _owner_from_url(repo_url: str) -> str:
    return path_from_url(repo_url).rpartition("/")[0]


def derive_name(
    rule: ModuleAutodiscoveryRule, subdirectory: str, repo: RepoContext | None = None
) -> str:
    """The registry name for the module at `subdirectory` of a repository.

    Without a template: the repository's module name, then the submodule's
    directory (`terraform-azurerm-mg` + `modules/create` → `mg-create`). A
    template may use `{repo}` (the repository's module name), `{path}` (the
    subdirectory with `/` as `-`), `{leaf}` (its last segment), `{root}` (the
    subdirectory as-is) and `{owner}` (the repository's account or group path,
    `/` as `-`). Either way the result is fitted to the registry's name rule.
    `repo` defaults to the rule's own repository.
    """
    repo_name = repo.name if repo is not None else repo_name_from_url(rule.repo_url)
    if not rule.name_template:
        return suggest_name(repo_name, subdirectory)
    owner = repo.owner if repo is not None else _owner_from_url(rule.repo_url)
    # Substituted with a regex, never `str.format`: the template is operator
    # input, and format specs or attribute access have no business in a name.
    values = {
        "repo": module_base_name(repo_name),
        "path": subdirectory.replace("/", "-"),
        "leaf": subdirectory.rsplit("/", 1)[-1] if subdirectory else "",
        "root": subdirectory,
        "owner": owner.replace("/", "-"),
    }
    rendered = TEMPLATE_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], rule.name_template)
    return fit_name(rendered)


def derive_provider(rule: ModuleAutodiscoveryRule, repo: RepoContext | None = None) -> str:
    """The rule's provider, or the one a conventional repository name implies."""
    if rule.provider:
        return rule.provider
    return suggest_provider(repo.name if repo is not None else repo_name_from_url(rule.repo_url))


# ── Preview ──────────────────────────────────────────────────────────────


def _url_key_sql(column):  # type: ignore[no-untyped-def]
    """`targets.url_key`, in SQL: so a module registered by hand at
    `…/Repo.git/` still counts as registered from `…/repo`."""
    return func.regexp_replace(func.lower(func.btrim(column)), r"(\.git)?/*$", "")


async def _registered_by_repository(
    db: AsyncSession, contexts: list[RepoContext]
) -> dict[str, dict[str, dict]]:
    """Modules already registered from each repository, by subdirectory.

    Keyed by each context's `key`. URLs are compared normalised — case, `.git`
    and trailing slashes aside — and a renamed repository's old URLs count.
    """
    lookup = {targets.url_key(u): ctx.key for ctx in contexts for u in ctx.urls}
    out: dict[str, dict[str, dict]] = {ctx.key: {} for ctx in contexts}
    if not lookup:
        return out
    rows = await db.execute(
        select(
            RegistryModule.name,
            RegistryModule.provider,
            RegistryModule.subdirectory,
            RegistryModule.vcs_repo_url,
        ).where(_url_key_sql(RegistryModule.vcs_repo_url).in_(sorted(lookup)))
    )
    for name, provider, subdirectory, *rest in rows.all():
        if rest:
            owner = lookup.get(targets.url_key(rest[0]))
        else:
            owner = contexts[0].key if len(contexts) == 1 else None
        if owner is not None:
            out[owner].setdefault(subdirectory or "", {"name": name, "provider": provider})
    return out


async def _taken_names(db: AsyncSession, provider: str, names: set[str]) -> set[str]:
    rows = await db.execute(
        select(RegistryModule.name).where(
            RegistryModule.namespace == "default",
            RegistryModule.provider == provider,
            RegistryModule.name.in_(names),
        )
    )
    return {name for (name,) in rows.all()}


async def _entries(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    groups: list[tuple[RepoContext, list[str]]],
) -> list[dict]:
    """Preview entries for candidates in one or more repositories.

    `registered-as` names the module already registered from that directory of
    that repository; a scan skips it. `collision` is true when the derived name
    and provider already belong to a different module, or when another
    unregistered candidate in the same set — from any of the repositories —
    derives the same name and provider; a scan skips every one of those, since
    registering one would take the name from the others. `missing-provider` is
    true when no provider is set and the repository name does not imply one.
    """
    groups = [(ctx, subs) for ctx, subs in groups if subs]
    if not groups:
        return []
    registered = await _registered_by_repository(db, [ctx for ctx, _ in groups])
    planned = []  # (ctx, subdirectory, name, provider, registered-as)
    names_by_provider: dict[str, set[str]] = defaultdict(set)
    for ctx, subs in groups:
        provider = derive_provider(rule, ctx)
        for d in subs:
            name = derive_name(rule, d, ctx)
            planned.append((ctx, d, name, provider, registered[ctx.key].get(d)))
            if provider:
                names_by_provider[provider].add(name)
    taken = {p: await _taken_names(db, p, names) for p, names in names_by_provider.items()}
    # Name and provider pairs two or more unregistered candidates would both take.
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for _ctx, _d, name, provider, existing in planned:
        if existing is None:
            counts[(name, provider)] += 1

    return [
        {
            "repository": ctx.path,
            "repo-url": ctx.url,
            "subdirectory": d,
            "name": name,
            "provider": provider,
            "registered-as": existing,
            "collision": existing is None
            and (name in taken.get(provider, set()) or counts[(name, provider)] > 1),
            "missing-provider": not provider,
        }
        for ctx, d, name, provider, existing in planned
    ]


async def preview(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    file_paths: list[str],
    *,
    repo: RepoContext | None = None,
) -> list[dict]:
    """The candidates in one repository, each with what a scan would do with it.

    `repo` is the repository the file paths came from; it defaults to the
    rule's own, for a single-repository rule. See `_entries` for the flags.
    """
    ctx = repo if repo is not None else rule_context(rule)
    return await _entries(db, rule, [(ctx, await candidates_off_loop(rule, file_paths))])


async def stored_preview(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    rows: list[ModuleAutodiscoveryRepository],
) -> list[dict]:
    """Entries for the candidates the poller last found in `rows`, with no VCS
    calls: an org-wide rule's preview, a page of repositories at a time."""
    return await _entries(db, rule, [(row_context(r), stored_subdirectories(r)) for r in rows])


# ── Scan (register) ──────────────────────────────────────────────────────


class UnknownSubdirectoryError(ValueError):
    """A requested subdirectory is not one of the rule's candidates."""


@dataclass
class ScanResult:
    created: list[RegistryModule] = field(default_factory=list)
    #: (subdirectory, reason) for each candidate that was not registered.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: The repository each created module came from, in `created`'s order.
    created_in: list[str] = field(default_factory=list)
    #: `{repository, repo-url, subdirectory, reason}`, in `skipped`'s order.
    skipped_in: list[dict] = field(default_factory=list)


async def register_candidates(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    file_paths: list[str],
    *,
    only: list[str] | None = None,
    repo: RepoContext | None = None,
) -> ScanResult:
    """Register the candidates in one repository — all of them, or just `only`.

    Idempotent: a directory already registered is skipped, and so is a name
    that is already taken, so running it twice registers nothing new. Each
    registration is its own savepoint, so a race with a concurrent scan skips
    that one module rather than failing the rest. Flushes; the caller commits.
    """
    entries = await preview(db, rule, file_paths, repo=repo)
    by_subdirectory = {e["subdirectory"]: e for e in entries}
    if only is not None:
        unknown = [d for d in only if d not in by_subdirectory]
        if unknown:
            raise UnknownSubdirectoryError(
                "not a candidate of this rule: " + ", ".join(repr(d) for d in unknown)
            )
        entries = [by_subdirectory[d] for d in dict.fromkeys(only)]
    return await _register_entries(db, rule, entries)


async def register_stored(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    selections: list[tuple[ModuleAutodiscoveryRepository, list[str] | None]],
) -> ScanResult:
    """Register stored candidates of several repositories: an org-wide scan.

    Each selection is a repository row and the subdirectories to register
    from it, or None for all of them. Every selection is checked before
    anything is registered, so an unknown subdirectory registers nothing.
    Name clashes are judged across every selected repository at once.
    """
    entries = await _entries(
        db, rule, [(row_context(row), stored_subdirectories(row)) for row, _ in selections]
    )
    by_repo: dict[str, dict[str, dict]] = defaultdict(dict)
    for e in entries:
        by_repo[e["repository"]][e["subdirectory"]] = e
    chosen: list[dict] = []
    for row, only in selections:
        candidates = stored_subdirectories(row)
        if only is not None:
            unknown = [d for d in only if d not in candidates]
            if unknown:
                raise UnknownSubdirectoryError(
                    f"not a candidate in {row.repo_path}: " + ", ".join(repr(d) for d in unknown)
                )
            picked = list(dict.fromkeys(only))
        else:
            picked = candidates
        chosen.extend(by_repo[row.repo_path][d] for d in picked if d in by_repo[row.repo_path])
    result = await _register_entries(db, rule, chosen)
    skips: dict[str, list[dict]] = defaultdict(list)
    for skip in result.skipped_in:
        skips[skip["repository"]].append(
            {"subdirectory": skip["subdirectory"], "reason": skip["reason"]}
        )
    for row, _ in selections:
        row.last_skips = skips.get(row.repo_path, [])
    return result


async def _register_entries(
    db: AsyncSession, rule: ModuleAutodiscoveryRule, entries: list[dict]
) -> ScanResult:
    labels, _stripped = sanitize_labels(rule.labels or {})
    result = ScanResult()

    def skip(entry: dict, reason: str) -> None:
        result.skipped.append((entry["subdirectory"], reason))
        result.skipped_in.append(
            {
                "repository": entry.get("repository") or "",
                "repo-url": entry.get("repo-url") or rule.repo_url,
                "subdirectory": entry["subdirectory"],
                "reason": reason,
            }
        )

    for entry in entries:
        subdirectory = entry["subdirectory"]
        repo_url = entry.get("repo-url") or rule.repo_url
        repository = entry.get("repository") or ""
        if entry["registered-as"] is not None:
            skip(entry, "already-registered")
            continue
        if entry["missing-provider"]:
            skip(entry, "missing-provider")
            continue
        if entry["collision"]:
            skip(entry, "name-taken")
            continue
        module = RegistryModule(
            namespace="default",
            name=entry["name"],
            provider=entry["provider"],
            status="pending",
            labels=dict(labels),
            owner_email=rule.owner_email or "",
            source="vcs",
            vcs_connection_id=rule.vcs_connection_id,
            vcs_repo_url=repo_url,
            vcs_branch=rule.branch,
            vcs_tag_pattern=rule.vcs_tag_pattern or "v*",
            subdirectory=subdirectory,
            module_autodiscovery_rule_id=rule.id,
        )
        try:
            async with db.begin_nested():
                db.add(module)
        except IntegrityError:
            # Someone else got there between the preview and here: either the
            # directory itself or, under another directory, the name.
            ctx = RepoContext(repo_url, repository)
            now_registered = (await _registered_by_repository(db, [ctx]))[ctx.key]
            skip(entry, "already-registered" if subdirectory in now_registered else "name-taken")
            continue
        result.created.append(module)
        result.created_in.append(repository)
        logger.info(
            "Module autodiscovery registered a module",
            rule_id=str(rule.id),
            module=f"default/{module.name}/{module.provider}",
            repository=repository,
            subdirectory=subdirectory,
        )
    return result


# ── Repository access ────────────────────────────────────────────────────


class RepositoryError(Exception):
    """The repository could not be read. `status` is the HTTP status to report."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class RepositoryHead:
    owner: str
    repo: str
    branch: str
    sha: str | None


async def resolve_head(conn: VCSConnection, repo_url: str, branch: str) -> RepositoryHead:
    """Parse the repository, resolve the branch (the default when empty) and its head."""
    from terrapod.services import github_service, gitlab_service

    if conn.provider not in ("github", "gitlab"):
        raise RepositoryError(422, f"unknown VCS provider: {conn.provider!r}")
    parsed = targets.owner_repo(conn, repo_url)
    if parsed is None:
        raise RepositoryError(422, f"cannot parse repo URL: {repo_url!r}")
    owner, repo = parsed

    target = branch
    if not target:
        try:
            if conn.provider == "gitlab":
                target = await gitlab_service.get_default_branch(conn, owner, repo) or ""
            else:
                target = await github_service.get_repo_default_branch(conn, owner, repo) or ""
        except Exception as exc:
            raise RepositoryError(502, f"failed to resolve the default branch: {exc}") from exc
        if not target:
            raise RepositoryError(502, "the VCS provider returned no default branch")

    try:
        if conn.provider == "gitlab":
            sha = await gitlab_service.get_branch_sha(conn, owner, repo, target)
        else:
            sha = await github_service.get_repo_branch_sha(conn, owner, repo, target)
    except Exception:
        sha = None
    return RepositoryHead(owner=owner, repo=repo, branch=target, sha=sha)


async def list_files(conn: VCSConnection, head: RepositoryHead) -> list[str]:
    """Every file path in the repository at the head's branch."""
    from terrapod.services import github_service, gitlab_service

    try:
        if conn.provider == "gitlab":
            # Strict: a listing error (missing branch, revoked token) raises
            # rather than coming back as None, which here means "truncated".
            paths = await gitlab_service.list_repo_tree(
                conn, head.owner, head.repo, head.branch, raise_on_error=True
            )
        else:
            paths = await github_service.list_repo_tree(conn, head.owner, head.repo, head.branch)
    except Exception as exc:
        raise RepositoryError(502, f"failed to list the repository tree: {exc}") from exc
    if paths is None:
        raise RepositoryError(
            413,
            "the VCS provider truncated this repository's tree, so it is too large to "
            "scan in one pass; register its modules individually instead",
        )
    return paths


# ── Per-repository state (#1620) ─────────────────────────────────────────

_GIT_SUFFIX = re.compile(r"(\.git)?/*$", re.IGNORECASE)


def path_from_url(repo_url: str, server_url: object = "", provider: object = "") -> str:
    """The `owner/repo` (or `group/subgroup/project`) a repository URL names.

    Strips the scheme and host (or the `git@host:` prefix), a GitLab instance's
    relative URL root, `.git` and trailing slashes. Falls back to the input, so
    a state row always has a path.
    """
    from urllib.parse import urlsplit

    server = server_url if isinstance(server_url, str) else ""
    url = (repo_url or "").strip()
    if url.startswith("git@") and ":" in url:
        path = url.split(":", 1)[1]
    elif "://" in url:
        path = url.split("://", 1)[1].partition("/")[2]
        root = urlsplit(server).path.strip("/") if provider == "gitlab" else ""
        if root and path.lower().startswith(root.lower() + "/"):
            path = path[len(root) + 1 :]
    else:
        path = url
    path = _GIT_SUFFIX.sub("", path.strip("/"))
    return path or url


def target_kind(rule: ModuleAutodiscoveryRule) -> str:
    """What the rule's `repo-url` names; rules saved before #1620 name a repository."""
    return rule.target_kind or targets.KIND_REPOSITORY


def _repositories_loaded(rule: ModuleAutodiscoveryRule) -> bool:
    """Whether `rule.repositories` can be read without a query.

    True for a rule selected with its repositories, and for one not yet
    persisted (its collection is simply empty). Reading it otherwise would
    raise rather than load, since this runs on an async session.
    """
    state = sa_inspect(rule)
    return not state.persistent or "repositories" not in state.unloaded


async def load_repositories(
    db: AsyncSession, rule: ModuleAutodiscoveryRule
) -> list[ModuleAutodiscoveryRepository]:
    """The rule's per-repository state rows, loading them if need be."""
    if not _repositories_loaded(rule):
        await db.execute(
            select(ModuleAutodiscoveryRule)
            .where(ModuleAutodiscoveryRule.id == rule.id)
            .options(selectinload(ModuleAutodiscoveryRule.repositories))
        )
    return list(rule.repositories)


def _has_scan_state(row: ModuleAutodiscoveryRepository) -> bool:
    return bool(row.last_scanned_sha or row.seen_subdirectories)


def _conn_bits(rule: ModuleAutodiscoveryRule) -> tuple[str, str]:
    conn = rule.vcs_connection
    server = getattr(conn, "server_url", "") if conn is not None else ""
    provider = getattr(conn, "provider", "") if conn is not None else ""
    return (server if isinstance(server, str) else ""), (
        provider if isinstance(provider, str) else ""
    )


def rule_context(
    rule: ModuleAutodiscoveryRule, row: ModuleAutodiscoveryRepository | None = None
) -> RepoContext:
    """A single-repository rule's repository, as modules are registered from it.

    The rule's own URL — or the canonical one, for a rule saved as a bare path —
    unless the repository has been renamed since, in which case its current URL,
    with the old ones still counting as the same repository.
    """
    server, provider = _conn_bits(rule)
    path = path_from_url(rule.repo_url, server, provider)
    url = rule.repo_url
    if not targets.is_full_url(url) and rule.vcs_connection is not None:
        url = targets.canonical_url(rule.vcs_connection, path)
    if row is not None and row.previous_paths:
        previous = (url, *(p.get("url") or "" for p in row.previous_paths))
        return RepoContext(row.repo_url, row.repo_path, tuple(u for u in previous if u))
    return RepoContext(url, path)


def row_context(row: ModuleAutodiscoveryRepository) -> RepoContext:
    """A repository an org-wide rule looks at, as modules are registered from it."""
    previous = tuple(p.get("url") or "" for p in (row.previous_paths or []) if p.get("url"))
    return RepoContext(row.repo_url, row.repo_path, previous)


def stored_subdirectories(row: ModuleAutodiscoveryRepository) -> list[str]:
    """The candidates the last scan of this repository found."""
    return [c["subdirectory"] for c in (row.candidates or []) if "subdirectory" in c]


def repository_state(rule: ModuleAutodiscoveryRule) -> ModuleAutodiscoveryRepository:
    """A repository-target rule's one state row, reconciled with the rule.

    Created when missing — for a rule saved after the migration — from the
    rule's own scan columns. Those columns are also what a replica on older
    code reads and writes during a rolling upgrade, so the row is reconciled
    with them: when the rule has no baseline but the row has state, an older
    replica re-baselined the rule (it knows nothing of the row), and the row
    starts afresh too. The collection must already be loaded.
    """
    rows = list(rule.repositories)
    row = rows[0] if rows else None
    now = datetime.now(UTC)
    if row is None:
        server, provider = _conn_bits(rule)
        row = ModuleAutodiscoveryRepository(
            repo_path=path_from_url(rule.repo_url, server, provider),
            repo_url=rule.repo_url,
            vcs_repo_id=rule.target_id or "",
            default_branch="",
            origin="baseline",
            status="active",
            change_marker="",
            last_scanned_sha=rule.last_scanned_sha or "",
            seen_subdirectories=list(rule.seen_subdirectories or []),
            candidates=[],
            last_skips=[],
            previous_paths=[],
            first_seen_at=rule.first_scan_at or now,
            failure_count=0,
            last_error="",
        )
        rule.repositories.append(row)
    elif rule.first_scan_at is None and _has_scan_state(row):
        row.last_scanned_sha = ""
        row.seen_subdirectories = []
        row.candidates = []
        row.last_skips = []
    return row


def _candidate_entries(
    rule: ModuleAutodiscoveryRule, subdirectories: list[str], repo: RepoContext | None = None
) -> list[dict]:
    provider = derive_provider(rule, repo)
    return [
        {"subdirectory": d, "name": derive_name(rule, d, repo), "provider": provider}
        for d in subdirectories
    ]


def _mark(
    row: ModuleAutodiscoveryRepository, status: str, now: datetime, marker: str | None = None
) -> None:
    """A check that went through: record it and put the row at the back of the queue."""
    row.status = status
    if marker is not None:
        row.change_marker = marker
    row.last_checked_at = now
    row.next_check_at = now
    row.failure_count = 0
    row.last_error = ""


def _backoff(failures: int) -> timedelta:
    return timedelta(
        seconds=min(_BACKOFF_BASE_SECONDS * 2 ** max(failures - 1, 0), _BACKOFF_MAX_SECONDS)
    )


def _fail(row: ModuleAutodiscoveryRepository, now: datetime, failures: int, detail: str) -> None:
    """A check that failed: back this repository off, doubling each time.

    `failures` is the count read before the check — the row may have been
    expired by a rolled-back savepoint since, and reading it would query.
    """
    row.status = "error"
    row.failure_count = failures + 1
    row.last_error = detail[:2000]
    row.last_checked_at = now
    row.next_check_at = now + _backoff(failures + 1)


# ── Poll ─────────────────────────────────────────────────────────────────


def record_scan(
    rule: ModuleAutodiscoveryRule,
    file_paths: list[str],
    head_sha: str | None,
    candidates: list[str] | None = None,
) -> None:
    """Note what the rule has now seen, so later polls take only what is new.

    Written to the rule's own columns and, when its state rows are loaded, to
    its repository row as well: both are kept current (#1620).

    An async caller that has already walked the tree passes ``candidates``:
    the walk is glob-matching over every path in the tree, which belongs on a
    worker thread (`candidates_off_loop`) rather than the event loop.
    """
    if candidates is None:
        candidates = candidate_subdirectories(rule, file_paths)
    seen = set(rule.seen_subdirectories or [])
    seen.update(candidates)
    rows = list(rule.repositories) if _repositories_loaded(rule) else []
    if rows:
        seen.update(rows[0].seen_subdirectories or [])
    rule.seen_subdirectories = sorted(seen)
    rule.last_scanned_sha = head_sha or ""
    rule.first_scan_at = rule.first_scan_at or datetime.now(UTC)
    if rows:
        row = rows[0]
        row.seen_subdirectories = sorted(seen)
        row.last_scanned_sha = head_sha or ""
        row.candidates = _candidate_entries(rule, candidates, rule_context(rule, row))
        _mark(row, "active", datetime.now(UTC))


@dataclass
class _Cycle:
    """What org-wide rules may still spend in this poll cycle."""

    deadline: float
    trees_left: int

    def spent(self) -> bool:
        return time.monotonic() >= self.deadline


async def _quota_share(conn: VCSConnection) -> float | None:
    """The share of the connection's API quota left, when known and current."""
    snapshot = await vcs_rate_limit.get_snapshot(conn.id)
    if snapshot is None or snapshot.limit <= 0:
        return None
    if snapshot.reset_at and snapshot.reset_at <= time.time():
        return None  # the budget has refilled since this was observed
    return snapshot.remaining / snapshot.limit


async def _covered(db: AsyncSession) -> dict:
    """What single-repository rules already name, per connection: `(paths, ids)`.

    An org-wide rule skips those repositories (`covered`), whether or not the
    single-repository rule is enabled, so no repository is handled by two rules.
    """
    rows = await db.execute(
        select(
            ModuleAutodiscoveryRule.vcs_connection_id,
            ModuleAutodiscoveryRule.repo_url,
            ModuleAutodiscoveryRule.target_id,
            VCSConnection.server_url,
            VCSConnection.provider,
        )
        .join(VCSConnection, VCSConnection.id == ModuleAutodiscoveryRule.vcs_connection_id)
        .where(ModuleAutodiscoveryRule.target_kind == targets.KIND_REPOSITORY)
    )
    out: dict = defaultdict(lambda: (set(), set()))
    for conn_id, repo_url, target_id, server_url, provider in rows.all():
        paths, ids = out[conn_id]
        paths.add(path_from_url(repo_url, server_url or "", provider or "").lower())
        if target_id:
            ids.add(target_id)
    return out


async def poll_rules(db: AsyncSession) -> int:
    """Register directories that are new to each enabled rule.

    A single-repository rule costs one branch-head lookup per cycle; its tree
    is walked only when the head differs from the last scan's. The first poll
    of a repository registers nothing: it records what is already there, which
    an operator registers with an explicit scan — all of it or a chosen subset.
    After that, a directory that appears on the tracked branch is registered
    automatically, and one the operator left unregistered stays that way.

    Org-wide rules are polled after every single-repository rule, within the
    limits in `settings.registry.module_autodiscovery`; see
    `_poll_namespace_rule`.

    Each rule runs in its own savepoint, so one that fails — an unreachable
    repository, a truncated tree, or a database error while registering or
    recording the scan — is rolled back on its own and left as it was for the
    next cycle to retry, and never takes the other rules' registrations with it.
    A rule whose VCS connection is not active is skipped, with a warning.
    Returns how many modules were registered. Flushes; the caller commits.
    """
    from terrapod.config import settings

    limits = settings.registry.module_autodiscovery
    rules = list(
        (
            await db.execute(
                select(ModuleAutodiscoveryRule)
                .where(ModuleAutodiscoveryRule.enabled.is_(True))
                .options(selectinload(ModuleAutodiscoveryRule.repositories))
            )
        )
        .scalars()
        .all()
    )
    # Single-repository rules first: the budget is for org-wide rules, and
    # must never hold up a rule that names one repository. The sort is stable.
    rules.sort(key=lambda r: target_kind(r) != targets.KIND_REPOSITORY)
    namespace_rules = any(target_kind(r) != targets.KIND_REPOSITORY for r in rules)
    covered = await _covered(db) if namespace_rules else {}
    cycle = _Cycle(
        deadline=time.monotonic() + limits.time_budget_seconds,
        trees_left=limits.tree_listings_per_cycle,
    )

    registered = 0
    for rule in rules:
        # Read before the savepoint: a rolled-back savepoint expires the rule.
        rule_id, rule_name = str(rule.id), rule.name
        conn = rule.vcs_connection
        if conn is None or conn.status != "active":
            logger.warning(
                "Module autodiscovery rule skipped: its VCS connection is not active",
                rule_id=rule_id,
                rule_name=rule_name,
                connection_status=conn.status if conn is not None else None,
            )
            continue
        try:
            async with db.begin_nested():
                if target_kind(rule) == targets.KIND_REPOSITORY:
                    registered += await _poll_rule(db, rule, conn)
                else:
                    registered += await _poll_namespace_rule(
                        db,
                        rule,
                        conn,
                        cycle,
                        covered.get(rule.vcs_connection_id, (set(), set())),
                        limits,
                    )
        except Exception:
            logger.warning(
                "Module autodiscovery poll failed for rule",
                rule_id=rule_id,
                rule_name=rule_name,
                exc_info=True,
            )
    return registered


# ── Poll: a single-repository rule ───────────────────────────────────────


async def _follow_rename(
    conn: VCSConnection, rule: ModuleAutodiscoveryRule, row: ModuleAutodiscoveryRepository
) -> tuple[RepoContext | None, str]:
    """Look the rule's repository up by id after its URL failed.

    Returns its new context when it has moved, recording the old path on the
    row — the modules already registered keep their URL, and still count as
    registered from it. Otherwise `(None, why)`.
    """
    try:
        ref = await targets.repository_by_id(conn, rule.target_id)
    except Exception as exc:
        return None, f"could not look the repository up by id: {exc}"
    if ref is None:
        return None, f"the repository this rule names (id {rule.target_id}) no longer exists"
    if ref.path.lower() == row.repo_path.lower():
        return None, ""
    previous = list(row.previous_paths or [])
    previous.append({"path": row.repo_path, "url": row.repo_url})
    row.previous_paths = previous
    row.repo_path, row.repo_url = ref.path, ref.url
    row.vcs_repo_id = ref.id
    logger.info(
        "Module autodiscovery rule's repository was renamed; following it",
        rule_id=str(rule.id),
        old_path=previous[-1]["path"],
        new_path=ref.path,
    )
    return rule_context(rule, row), ""


async def _poll_rule(db: AsyncSession, rule: ModuleAutodiscoveryRule, conn: VCSConnection) -> int:
    """One single-repository rule's poll: register what is new and record the scan.

    A repository that cannot be read leaves the rule as it was, with the reason
    in `last_error`. When the rule knows its repository's id, a failed read is
    followed by a lookup by id, so a renamed or transferred repository keeps
    being polled.
    """
    row = repository_state(rule)

    async def read(ctx: RepoContext) -> tuple[RepoContext, RepositoryHead, list[str] | None]:
        head = await resolve_head(conn, ctx.url, rule.branch)
        # Either copy of the head counts: an older replica during a rolling
        # upgrade writes only the rule's, and it recorded what it saw there.
        scanned = {rule.last_scanned_sha, row.last_scanned_sha} - {""}
        if rule.first_scan_at and head.sha and head.sha in scanned:
            return ctx, head, None
        return ctx, head, await list_files(conn, head)

    try:
        with vcs_rate_limit.vcs_target(
            consumer=f"module-rule/{rule.name}", kind="module-rule", labels=rule.labels
        ):
            try:
                ctx, head, file_paths = await read(rule_context(rule, row))
            except RepositoryError as exc:
                if not rule.target_id:
                    raise
                moved, why = await _follow_rename(conn, rule, row)
                if moved is None:
                    raise RepositoryError(exc.status, why or exc.detail) from exc
                ctx, head, file_paths = await read(moved)
    except RepositoryError as exc:
        # Left as it was for the next cycle; the reason is on the rule.
        rule.last_error = row.last_error = exc.detail
        logger.warning(
            "Module autodiscovery could not read a rule's repository",
            rule_id=str(rule.id),
            rule_name=rule.name,
            detail=exc.detail,
        )
        return 0
    if file_paths is None:
        rule.last_error = ""
        return 0
    created = 0
    cands = await candidates_off_loop(rule, file_paths)
    if rule.first_scan_at is not None:
        seen = set(rule.seen_subdirectories or []) | set(row.seen_subdirectories or [])
        new = [d for d in cands if d not in seen]
        if new:
            result = await register_candidates(db, rule, file_paths, only=new, repo=ctx)
            created = len(result.created)
            row.last_skips = [{"subdirectory": d, "reason": r} for d, r in result.skipped]
    row.default_branch = head.branch
    rule.last_error = ""
    record_scan(rule, file_paths, head.sha, candidates=cands)
    await db.flush()
    return created


# ── Poll: an org-wide rule ───────────────────────────────────────────────


def _origin(ref: RepositoryRef, baseline_at: datetime | None) -> str:
    """`new` for a repository created after the rule took its baseline."""
    if baseline_at is not None and ref.created_at is not None and ref.created_at > baseline_at:
        return "new"
    return "baseline"


def _reset_row(row: ModuleAutodiscoveryRepository) -> None:
    row.last_scanned_sha = ""
    row.seen_subdirectories = []
    row.candidates = []
    row.last_skips = []
    row.change_marker = ""
    row.last_checked_at = None
    row.next_check_at = None
    row.failure_count = 0
    row.last_error = ""


def reconcile(
    rule: ModuleAutodiscoveryRule,
    listing: RepositoryListing,
    covered: tuple[set[str], set[str]],
    now: datetime,
) -> list[tuple[ModuleAutodiscoveryRepository, RepositoryRef]]:
    """Bring the rule's repository rows in line with a listing. Pure; no I/O.

    - A repository new to the rule gets a row: `new` if it was created after
      the rule's baseline, `baseline` otherwise (it already existed, and moved
      into scope — added to the App's selection, transferred in).
    - A renamed repository is matched by id; its row takes the new path and
      keeps the old one in `previous_paths`. Modules are never repointed.
    - Forks and disabled repositories are not in scope. An archived one keeps
      its state but is not scanned (`archived`); one a single-repository rule
      on the connection already names is skipped (`covered`).
    - Only a complete listing marks a repository that is absent from it as
      `out-of-scope`; a truncated or failed one says nothing about absence.

    Returns each in-scope repository with its row.
    """
    covered_paths, covered_ids = covered
    rows = list(rule.repositories)
    by_id = {r.vcs_repo_id: r for r in rows if r.vcs_repo_id}
    by_path = {r.repo_path.lower(): r for r in rows}
    baseline_at = rule.first_scan_at
    present: set[int] = set()
    pairs = []
    for ref in listing.repositories:
        if ref.fork or ref.disabled:
            continue
        row = by_id.get(ref.id)
        if row is None:
            row = by_path.get(ref.path.lower())
            if row is not None and row.vcs_repo_id and row.vcs_repo_id != ref.id:
                # Another repository now holds a path this rule had a row for:
                # the old one went. The row starts afresh for the new one.
                _reset_row(row)
                row.origin = _origin(ref, baseline_at)
                row.status = "active"
                row.vcs_repo_id = ref.id
                by_id[ref.id] = row
        if row is None:
            row = ModuleAutodiscoveryRepository(
                repo_path=ref.path,
                repo_url=ref.url,
                vcs_repo_id=ref.id,
                default_branch=ref.default_branch,
                origin=_origin(ref, baseline_at),
                status="active",
                change_marker="",
                last_scanned_sha="",
                seen_subdirectories=[],
                candidates=[],
                last_skips=[],
                previous_paths=[],
                repo_created_at=ref.created_at,
                first_seen_at=now,
                failure_count=0,
                last_error="",
            )
            rule.repositories.append(row)
            rows.append(row)
            by_id[ref.id] = row
            by_path[ref.path.lower()] = row
        elif row.repo_path != ref.path:
            if row.repo_path.lower() != ref.path.lower():
                holder = by_path.get(ref.path.lower())
                if holder is not None and holder is not row:
                    # A stale row still holds the new path; move it aside.
                    holder.repo_path = f"{holder.repo_path}#{holder.vcs_repo_id or 'gone'}"
                    by_path[holder.repo_path.lower()] = holder
                previous = list(row.previous_paths or [])
                previous.append({"path": row.repo_path, "url": row.repo_url})
                row.previous_paths = previous
                by_path.pop(row.repo_path.lower(), None)
            row.repo_path = ref.path
            by_path[ref.path.lower()] = row
        row.vcs_repo_id = row.vcs_repo_id or ref.id
        row.repo_url = ref.url or row.repo_url
        row.default_branch = ref.default_branch
        row.repo_created_at = row.repo_created_at or ref.created_at
        present.add(id(row))

        if ref.path.lower() in covered_paths or ref.id in covered_ids:
            row.status = "covered"
        elif ref.archived:
            row.status = "archived"
        elif row.status in ("covered", "archived", "out-of-scope"):
            row.status = "active"
        pairs.append((row, ref))

    if listing.complete:
        for row in rows:
            if id(row) not in present:
                row.status = "out-of-scope"
    return pairs


def _is_due(row: ModuleAutodiscoveryRepository, ref: RepositoryRef, now: datetime) -> bool:
    if row.status in ("covered", "archived"):
        return False
    if row.next_check_at is not None and row.next_check_at > now:
        return False  # backing off
    if row.status == "error" or row.last_checked_at is None:
        return True
    return row.change_marker != ref.change_marker


def due_repositories(
    pairs: list[tuple[ModuleAutodiscoveryRepository, RepositoryRef]], now: datetime
) -> list[tuple[ModuleAutodiscoveryRepository, RepositoryRef]]:
    """The repositories worth a look, round-robin: never checked first, then
    least recently checked. Only those whose change marker moved — or that
    failed and have waited out their backoff — are due."""
    due = [(row, ref) for row, ref in pairs if _is_due(row, ref, now)]
    due.sort(
        key=lambda p: (
            p[0].next_check_at is not None,
            p[0].next_check_at or now,
            p[0].repo_path.lower(),
        )
    )
    return due


@dataclass
class _TreeScan:
    row: ModuleAutodiscoveryRepository
    ref: RepositoryRef
    sha: str
    candidates: list[str]


async def _check_repository(
    conn: VCSConnection,
    rule: ModuleAutodiscoveryRule,
    row: ModuleAutodiscoveryRepository,
    ref: RepositoryRef,
    cycle: _Cycle,
    now: datetime,
) -> _TreeScan | None:
    """Look at one repository: its branch head, and its tree if the head moved.

    Only reads the provider; what it finds is recorded later, in a savepoint.
    A repository that cannot be read is backed off, and the rule goes on.
    """
    from terrapod.services import vcs_provider

    failures = row.failure_count or 0
    try:
        with vcs_rate_limit.vcs_target(
            repo=ref.path, consumer=f"module-rule/{rule.name}", kind="module-rule"
        ):
            branch = rule.branch or ref.default_branch
            if ref.empty or not branch:
                # Retried when it changes, so modules register as they arrive.
                _mark(row, "empty", now, ref.change_marker)
                return None
            owner, name = ref.path.rpartition("/")[0], ref.name
            try:
                sha = await vcs_provider.get_branch_sha(conn, owner, name, branch)
            except Exception as exc:
                raise RepositoryError(502, f"failed to read branch {branch!r}: {exc}") from exc
            if sha is None:
                _mark(row, "no-branch", now, ref.change_marker)
                return None
            if sha == row.last_scanned_sha:
                _mark(row, "active", now, ref.change_marker)
                return None
            cycle.trees_left -= 1
            paths = await list_files(conn, RepositoryHead(owner, name, branch, sha))
    except RepositoryError as exc:
        _fail(row, now, failures, exc.detail)
        return None
    except Exception as exc:
        _fail(row, now, failures, str(exc))
        return None
    return _TreeScan(row, ref, sha, await candidates_off_loop(rule, paths))


async def _record_scans(
    db: AsyncSession, rule: ModuleAutodiscoveryRule, scans: list[_TreeScan], now: datetime
) -> int:
    """Register what is new in this cycle's scans and record each one.

    Name clashes are judged across every repository scanned this cycle, so a
    clash skips every candidate involved rather than letting whichever came
    first win. Each repository is recorded in its own savepoint: a database
    error in one backs that repository off and leaves the rest recorded.
    """
    plans = []
    for scan in scans:
        seen = set(scan.row.seen_subdirectories or [])
        # A `new` repository registers everything it holds. A baseline one
        # registers only after its first scan, which records what was there.
        registers = scan.row.origin == "new" or bool(scan.row.last_scanned_sha)
        to_register = [d for d in scan.candidates if d not in seen] if registers else []
        plans.append((scan, row_context(scan.row), seen, to_register))

    groups = [(ctx, scan.candidates) for scan, ctx, _seen, todo in plans if todo]
    by_repo: dict[str, dict[str, dict]] = defaultdict(dict)
    for entry in await _entries(db, rule, groups):
        by_repo[entry["repository"]][entry["subdirectory"]] = entry

    created = 0
    for scan, ctx, seen, to_register in plans:
        row = scan.row
        failures = row.failure_count or 0
        chosen = [by_repo[ctx.path][d] for d in to_register if d in by_repo[ctx.path]]
        try:
            async with db.begin_nested():
                result = await _register_entries(db, rule, chosen)
                row.last_skips = [
                    {"subdirectory": d, "reason": reason} for d, reason in result.skipped
                ]
                row.seen_subdirectories = sorted(seen | set(scan.candidates))
                row.last_scanned_sha = scan.sha
                row.candidates = _candidate_entries(rule, scan.candidates, ctx)
                _mark(row, "active", now, scan.ref.change_marker)
                await db.flush()
        except Exception as exc:
            try:
                await db.refresh(row)
            except Exception:
                logger.debug("Could not refresh a repository row", exc_info=True)
            _fail(row, now, failures, f"could not record the scan: {exc}")
            logger.warning(
                "Module autodiscovery could not record a repository's scan",
                rule_id=str(rule.id),
                repository=ctx.path,
                exc_info=True,
            )
            continue
        created += len(result.created)
    return created


async def _poll_namespace_rule(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    conn: VCSConnection,
    cycle: _Cycle,
    covered: tuple[set[str], set[str]],
    limits,  # type: ignore[no-untyped-def]
) -> int:
    """One org-wide rule's poll.

    Lists the namespace's repositories every cycle (conditionally, where the
    provider allows), reconciles the rule's rows with the listing, then looks
    at the repositories that changed: a branch-head lookup for each whose
    change marker moved, and a tree listing only where the head did.

    Bounded, in this order: below the enumeration quota floor the rule is
    skipped for the cycle; below the tree floor, and once the cycle's tree
    listings or wall-clock budget are spent, it stops looking at repositories
    and leaves the rest, least recently checked first, for the next cycle.
    The rule's `last_error` says why it could not do its work, or is cleared.
    """
    share = await _quota_share(conn)
    if share is not None and share * 100 < limits.enumeration_quota_floor_percent:
        rule.last_error = (
            f"skipped: {share:.0%} of the connection's API quota is left, below the "
            f"{limits.enumeration_quota_floor_percent}% floor for listing repositories"
        )
        return 0
    if cycle.spent():
        return 0
    try:
        glob = targets.split_glob(targets.normalise(conn, rule.repo_url))[1]
    except targets.TargetError as exc:
        rule.last_error = exc.detail
        return 0

    with vcs_rate_limit.vcs_target(
        consumer=f"module-rule/{rule.name}", kind="module-rule", labels=rule.labels
    ):
        try:
            listing = await targets.list_repositories(
                conn,
                target_kind(rule),
                rule.target_id,
                glob,
                max_repositories=limits.max_repositories,
            )
        except targets.TargetGone as exc:
            rule.last_error = str(exc)
            return 0
        except Exception as exc:
            rule.last_error = f"could not list the repositories: {exc}"
            logger.warning(
                "Module autodiscovery could not list a rule's repositories",
                rule_id=str(rule.id),
                exc_info=True,
            )
            return 0

    now = datetime.now(UTC)
    pairs = reconcile(rule, listing, covered, now)
    rule.last_error = (
        ""
        if listing.complete
        else f"the listing stopped at {limits.max_repositories} repositories, so no "
        "repository is marked out of scope"
    )
    if listing.complete:
        rule.last_enumerated_at = now
    # The baseline: a repository created after this registers everything.
    rule.first_scan_at = rule.first_scan_at or now
    await db.flush()

    scans: list[_TreeScan] = []
    for row, ref in due_repositories(pairs, now):
        if cycle.spent() or cycle.trees_left <= 0:
            break
        share = await _quota_share(conn)
        if share is not None and share * 100 < limits.tree_quota_floor_percent:
            rule.last_error = (
                f"paused: {share:.0%} of the connection's API quota is left, below the "
                f"{limits.tree_quota_floor_percent}% floor for reading repositories"
            )
            break
        scan = await _check_repository(conn, rule, row, ref, cycle, now)
        if scan is not None:
            scans.append(scan)
    return await _record_scans(db, rule, scans, now) if scans else 0


# ── Previews of repositories the rule has not saved (#1620) ──────────────


async def read_repositories(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    conn: VCSConnection,
    refs: list[RepositoryRef],
    covered: tuple[set[str], set[str]] = (set(), set()),
) -> tuple[list[dict], list[dict], int]:
    """Read `refs` live and preview their candidates: an unsaved org-wide rule.

    Returns `(entries, repositories, files_walked)`, where each repository
    carries its status, and the error when it could not be read — shown inline,
    never failing the whole preview.
    """
    groups: list[tuple[RepoContext, list[str]]] = []
    infos: list[dict] = []
    walked = 0
    covered_paths, covered_ids = covered
    for ref in refs:
        branch = rule.branch or ref.default_branch
        info = {
            "repository": ref.path,
            "repo-url": ref.url,
            "ref": branch,
            "status": "active",
            "error": "",
        }
        infos.append(info)
        if ref.path.lower() in covered_paths or ref.id in covered_ids:
            info["status"] = "covered"
            continue
        if ref.archived:
            info["status"] = "archived"
            continue
        if ref.empty or not branch:
            info["status"] = "empty"
            continue
        owner, name = ref.path.rpartition("/")[0], ref.name
        try:
            with vcs_rate_limit.vcs_target(repo=ref.path):
                paths = await list_files(conn, RepositoryHead(owner, name, branch, None))
        except RepositoryError as exc:
            info["status"], info["error"] = "error", exc.detail
            continue
        walked += len(paths)
        groups.append((RepoContext(ref.url, ref.path), await candidates_off_loop(rule, paths)))
    return await _entries(db, rule, groups), infos, walked


async def covered_for(db: AsyncSession, conn_id) -> tuple[set[str], set[str]]:  # type: ignore[no-untyped-def]
    """What single-repository rules on one connection already name."""
    return (await _covered(db)).get(conn_id, (set(), set()))
