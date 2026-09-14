"""Module autodiscovery (#1584): rules that find modules in a repository and
register them in the private registry.

The module registry's counterpart to workspace autodiscovery. A rule names one
repository on a VCS connection, a glob `pattern` (minus `ignore_patterns`) over
its Terraform files, and how each module is named. Every matching file's
directory is a candidate — the repository root or a submodule (#1583):

- **preview** proposes the candidates and registers nothing;
- **scan** registers them, all or a chosen subset, through the ordinary
  module-create fields, each with its `subdirectory`;
- **poll** (from the registry VCS poll cycle) registers new candidates when the
  tracked branch moves, so a submodule added to the repository is picked up
  without anyone touching the registry.

Nothing here ever deletes or renames a module: a directory that disappears
simply stops producing versions, because tags without it are already skipped.

Patterns are the same gitignore-style globs as workspace rules, matched against
file paths. On top of them, directories that conventionally hold something other
than a module to publish (examples, tests, fixtures, hidden directories) are
never candidates, and only `.tf`/`.tf.json` files count — a directory of
`.tfvars` is a root configuration, not a module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import ModuleAutodiscoveryRule, RegistryModule, VCSConnection
from terrapod.logging_config import get_logger
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
from terrapod.services.workspace_autodiscovery_service import _is_ignored, _match_glob

logger = get_logger(__name__)

_MODULE_FILE_SUFFIXES = (".tf", ".tf.json")


# ── Candidates ───────────────────────────────────────────────────────────


def rule_claims_path(rule: ModuleAutodiscoveryRule, path: str) -> bool:
    """Whether the rule counts the file at `path` towards a module. Pure."""
    if not path.endswith(_MODULE_FILE_SUFFIXES):
        return False
    if _is_ignored(path, rule.ignore_patterns or []):
        return False
    return _match_glob(path, rule.pattern)


def candidate_subdirectories(rule: ModuleAutodiscoveryRule, file_paths: list[str]) -> list[str]:
    """The module directories the rule claims, the repository root first."""
    return candidate_directories([p for p in file_paths if rule_claims_path(rule, p)])


def derive_name(rule: ModuleAutodiscoveryRule, subdirectory: str) -> str:
    """The registry name for the module at `subdirectory`.

    Without a template: the repository's module name, then the submodule's
    directory (`terraform-azurerm-mg` + `modules/create` → `mg-create`). A
    template may use `{repo}` (the repository's module name), `{path}` (the
    subdirectory with `/` as `-`), `{leaf}` (its last segment) and `{root}`
    (the subdirectory as-is). Either way the result is fitted to the registry's
    name rule.
    """
    repo_name = repo_name_from_url(rule.repo_url)
    if not rule.name_template:
        return suggest_name(repo_name, subdirectory)
    rendered = rule.name_template.format(
        repo=module_base_name(repo_name),
        path=subdirectory.replace("/", "-"),
        leaf=subdirectory.rsplit("/", 1)[-1] if subdirectory else "",
        root=subdirectory,
    )
    return fit_name(rendered)


def derive_provider(rule: ModuleAutodiscoveryRule) -> str:
    """The rule's provider, or the one a conventional repository name implies."""
    return rule.provider or suggest_provider(repo_name_from_url(rule.repo_url))


# ── Preview ──────────────────────────────────────────────────────────────


async def preview(
    db: AsyncSession, rule: ModuleAutodiscoveryRule, file_paths: list[str]
) -> list[dict]:
    """The rule's candidates, each with what a scan would do with it.

    `registered-as` names the module already registered from that directory of
    the repository; a scan skips it. `collision` is true when the derived name
    and provider already belong to a different module; a scan skips that too.
    `missing-provider` is true when no provider is set and the repository name
    does not imply one.
    """
    subdirectories = candidate_subdirectories(rule, file_paths)
    if not subdirectories:
        return []
    registered = await _registered_by_subdirectory(db, rule.repo_url)
    provider = derive_provider(rule)
    names = {d: derive_name(rule, d) for d in subdirectories}
    taken = await _taken_names(db, provider, set(names.values())) if provider else set()

    entries = []
    for d in subdirectories:
        existing = registered.get(d)
        entries.append(
            {
                "subdirectory": d,
                "name": names[d],
                "provider": provider,
                "registered-as": existing,
                "collision": existing is None and names[d] in taken,
                "missing-provider": not provider,
            }
        )
    return entries


async def _registered_by_subdirectory(db: AsyncSession, repo_url: str) -> dict[str, dict]:
    rows = await db.execute(
        select(RegistryModule.name, RegistryModule.provider, RegistryModule.subdirectory).where(
            RegistryModule.vcs_repo_url == repo_url
        )
    )
    out: dict[str, dict] = {}
    for name, provider, subdirectory in rows.all():
        out.setdefault(subdirectory or "", {"name": name, "provider": provider})
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


# ── Scan (register) ──────────────────────────────────────────────────────


class UnknownSubdirectoryError(ValueError):
    """A requested subdirectory is not one of the rule's candidates."""


@dataclass
class ScanResult:
    created: list[RegistryModule] = field(default_factory=list)
    #: (subdirectory, reason) for each candidate that was not registered.
    skipped: list[tuple[str, str]] = field(default_factory=list)


async def register_candidates(
    db: AsyncSession,
    rule: ModuleAutodiscoveryRule,
    file_paths: list[str],
    *,
    only: list[str] | None = None,
) -> ScanResult:
    """Register the rule's candidates — all of them, or just `only`.

    Idempotent: a directory already registered is skipped, and so is a name
    that is already taken, so running it twice registers nothing new. Each
    registration is its own savepoint, so a race with a concurrent scan skips
    that one module rather than failing the rest. Flushes; the caller commits.
    """
    entries = await preview(db, rule, file_paths)
    by_subdirectory = {e["subdirectory"]: e for e in entries}
    if only is not None:
        unknown = [d for d in only if d not in by_subdirectory]
        if unknown:
            raise UnknownSubdirectoryError(
                "not a candidate of this rule: " + ", ".join(repr(d) for d in unknown)
            )
        entries = [by_subdirectory[d] for d in dict.fromkeys(only)]

    labels, _stripped = sanitize_labels(rule.labels or {})
    result = ScanResult()
    for entry in entries:
        subdirectory = entry["subdirectory"]
        if entry["registered-as"] is not None:
            result.skipped.append((subdirectory, "already-registered"))
            continue
        if entry["missing-provider"]:
            result.skipped.append((subdirectory, "missing-provider"))
            continue
        if entry["collision"]:
            result.skipped.append((subdirectory, "name-taken"))
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
            vcs_repo_url=rule.repo_url,
            vcs_branch=rule.branch,
            vcs_tag_pattern=rule.vcs_tag_pattern or "v*",
            subdirectory=subdirectory,
            module_autodiscovery_rule_id=rule.id,
        )
        try:
            async with db.begin_nested():
                db.add(module)
        except IntegrityError:
            # Registered by someone else between the preview and here.
            result.skipped.append((subdirectory, "already-registered"))
            continue
        result.created.append(module)
        logger.info(
            "Module autodiscovery registered a module",
            rule_id=str(rule.id),
            module=f"default/{module.name}/{module.provider}",
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

    if conn.provider == "gitlab":
        parsed = gitlab_service.parse_repo_url(repo_url)
    elif conn.provider == "github":
        parsed = github_service.parse_repo_url(repo_url)
    else:
        raise RepositoryError(422, f"unknown VCS provider: {conn.provider!r}")
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
            paths = await gitlab_service.list_repo_tree(conn, head.owner, head.repo, head.branch)
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


# ── Poll ─────────────────────────────────────────────────────────────────


def record_scan(rule: ModuleAutodiscoveryRule, file_paths: list[str], head_sha: str | None) -> None:
    """Note what the rule has now seen, so later polls take only what is new."""
    seen = set(rule.seen_subdirectories or [])
    seen.update(candidate_subdirectories(rule, file_paths))
    rule.seen_subdirectories = sorted(seen)
    rule.last_scanned_sha = head_sha or ""
    rule.first_scan_at = rule.first_scan_at or datetime.now(UTC)


async def poll_rules(db: AsyncSession) -> int:
    """Register directories that are new to each enabled rule.

    One branch-head lookup per rule per cycle; the tree is walked only when the
    head differs from the last scan's. The first poll of a rule registers
    nothing: it records what is already there, which an operator registers with
    an explicit scan — all of it or a chosen subset. After that, a directory
    that appears on the tracked branch is registered automatically, and one the
    operator left unregistered stays that way.

    A rule that fails (unreachable repository, truncated tree) is left as it was
    so the next cycle retries it, and never stops the others. Returns how many
    modules were registered. Flushes; the caller commits.
    """
    rules = list(
        (
            await db.execute(
                select(ModuleAutodiscoveryRule).where(ModuleAutodiscoveryRule.enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    registered = 0
    for rule in rules:
        conn = rule.vcs_connection
        if conn is None or conn.status != "active":
            continue
        try:
            with vcs_rate_limit.vcs_target(
                consumer=f"module-rule/{rule.name}", kind="module-rule", labels=rule.labels
            ):
                head = await resolve_head(conn, rule.repo_url, rule.branch)
                if rule.first_scan_at and head.sha and head.sha == rule.last_scanned_sha:
                    continue
                file_paths = await list_files(conn, head)
                if rule.first_scan_at is not None:
                    seen = set(rule.seen_subdirectories or [])
                    new = [d for d in candidate_subdirectories(rule, file_paths) if d not in seen]
                    if new:
                        result = await register_candidates(db, rule, file_paths, only=new)
                        registered += len(result.created)
        except Exception:
            logger.warning(
                "Module autodiscovery poll failed for rule",
                rule_id=str(rule.id),
                rule_name=rule.name,
                exc_info=True,
            )
            continue
        record_scan(rule, file_paths, head.sha)
        await db.flush()
    return registered
