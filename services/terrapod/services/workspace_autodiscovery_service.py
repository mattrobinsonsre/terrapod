"""Workspace autodiscovery — Atlantis-style.

When a VCS poll detects changes to terraform files in a monorepo and
none of the existing workspaces' `trigger_prefixes` claim the path,
this service consults `AutodiscoveryRule` rows for the connection. A
rule whose `pattern` matches the file (and whose `ignore_patterns`
don't) auto-creates a workspace pointed at the file's directory.

The auto-created workspace inherits the rule's template fields:
execution mode, agent pool, terraform version, resources, labels,
owner. It carries `autodiscovery_rule_id` so subsequent poll cycles
recognise it and don't recreate.

Pattern syntax (gitignore-style):
- `*`  matches any sequence within a single path segment
- `**` matches any number of path segments (including zero)
- `?`  matches one character
- Patterns are matched against the *full file path* (e.g.
  `accounts/alpha/network/main.tf`).

Only terraform files (`*.tf`, `*.tfvars`) trigger autodiscovery —
README and other non-terraform changes are filtered out before rule
evaluation.

See terrapod #283.
"""

from __future__ import annotations

import re
import uuid
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import AutodiscoveryRule, Workspace
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


# ── Pattern matching ─────────────────────────────────────────────────────

# File extensions that count as terraform configuration. Matches
# Atlantis's default `when_modified: ["*.tf*"]` semantics.
_TF_EXTENSIONS = (".tf", ".tfvars", ".tf.json", ".tfvars.json", ".hcl")


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob to a compiled regex.

    Semantics:
    - `**`  → match any number of path segments (including zero), with
              optional trailing slash
    - `*`   → match anything within a single path segment (no `/`)
    - `?`   → match a single non-`/` character
    - everything else is treated literally (re.escape)
    """
    # Walk the pattern and emit regex pieces. Two-pass to handle the
    # double-star token before single-star.
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*" and i + 1 < n and pattern[i + 1] == "*":
            # `**` — match zero or more path segments.
            # Consume an optional `/` immediately after.
            i += 2
            if i < n and pattern[i] == "/":
                # `**/` — match zero or more segments with trailing slash
                out.append(r"(?:.*/)?")
                i += 1
            else:
                # bare `**` — match anything, including across segments
                out.append(r".*")
        elif ch == "*":
            # Single segment wildcard.
            out.append(r"[^/]*")
            i += 1
        elif ch == "?":
            out.append(r"[^/]")
            i += 1
        elif ch == "[":
            # Character class — find the closing bracket and copy the
            # contents through. gitignore also allows negation via `!`.
            j = pattern.find("]", i + 1)
            if j == -1:
                # Unclosed — escape literally.
                out.append(re.escape(ch))
                i += 1
            else:
                cls = pattern[i + 1 : j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append(f"[{cls}]")
                i = j + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile(r"\A" + "".join(out) + r"\Z")


def _match_glob(path: str, pattern: str) -> bool:
    """Return True if `path` matches the glob `pattern`."""
    return bool(_glob_to_regex(pattern).match(path))


def _is_terraform_file(path: str) -> bool:
    """True if the path looks like a terraform configuration file."""
    return any(path.endswith(ext) for ext in _TF_EXTENSIONS)


def _is_ignored(path: str, ignore_patterns: list[str]) -> bool:
    """True if any ignore pattern matches the path."""
    return any(_match_glob(path, p) for p in ignore_patterns)


def rule_claims_path(rule: AutodiscoveryRule, path: str) -> bool:
    """Decide whether `rule` would auto-create a workspace for `path`.

    Pure-logic; no I/O. Three checks: terraform file, matches the
    rule's pattern, not ignored.
    """
    if not _is_terraform_file(path):
        return False
    if _is_ignored(path, rule.ignore_patterns or []):
        return False
    return _match_glob(path, rule.pattern)


# ── Workspace derivation ─────────────────────────────────────────────────


def derive_root_directory(file_path: str) -> str:
    """Compute the workspace's `working_directory` from a matched file.

    By Atlantis's convention the root is the directory containing the
    terraform file. We just return the dirname.

    >>> derive_root_directory("accounts/alpha/network/main.tf")
    'accounts/alpha/network'
    >>> derive_root_directory("main.tf")
    ''
    """
    parent = PurePosixPath(file_path).parent
    return "" if str(parent) == "." else str(parent)


# Workspaces are 1..90 chars; legal set is letters/digits/`-`/`_`.
# We map disallowed chars to `-` and trim to fit.
_NAME_SANITISE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def derive_workspace_name(rule: AutodiscoveryRule, root_directory: str) -> str:
    """Derive a workspace name from the rule + root directory.

    Default behaviour (no `name_template` set): replace each `/` in the
    root_directory with `-` and sanitise. e.g.
        accounts/alpha/network → accounts-alpha-network

    If `name_template` is set, it can contain `{path}` (dashed root_dir)
    or `{root}` (the root_dir as-is). Useful when a single rule wants
    a prefix:
        name_template = "ws-{path}"
        accounts/alpha/network → ws-accounts-alpha-network
    """
    dashed = root_directory.replace("/", "-") or rule.name
    if rule.name_template:
        candidate = rule.name_template.format(path=dashed, root=root_directory)
    else:
        candidate = dashed

    # Sanitise + truncate to the workspaces.name 90-char column limit.
    candidate = _NAME_SANITISE_RE.sub("-", candidate).strip("-")
    return candidate[:90] or rule.name[:90]


# ── Find-or-autocreate ───────────────────────────────────────────────────


async def find_or_autocreate_workspace(
    db: AsyncSession,
    rule: AutodiscoveryRule,
    root_directory: str,
) -> tuple[Workspace, bool]:
    """Look up the workspace this rule + directory should map to, or
    create it if it doesn't exist.

    Returns `(workspace, created)`.

    Idempotent — concurrent autodiscovery on the same rule + path
    will not create duplicates. We commit the new workspace
    immediately so subsequent poller passes within the same poll
    cycle see it.
    """
    # Lookup #1: any workspace already claiming the (connection, repo,
    # working_directory) tuple? If so we reuse it regardless of how it
    # was created (rule, manual, etc.) — autodiscovery never replaces
    # an explicit workspace.
    existing = await db.execute(
        select(Workspace).where(
            Workspace.vcs_connection_id == rule.vcs_connection_id,
            Workspace.vcs_repo_url == rule.repo_url,
            Workspace.working_directory == root_directory,
        )
    )
    ws = existing.scalar_one_or_none()
    if ws is not None:
        return ws, False

    name = derive_workspace_name(rule, root_directory)

    # Lookup #2: the derived name might collide with an unrelated
    # workspace (different repo or working_directory) — refuse and let
    # the operator pick a more specific `name_template`. We log and
    # skip rather than mangle the name silently.
    name_clash = await db.execute(select(Workspace).where(Workspace.name == name))
    if name_clash.scalar_one_or_none() is not None:
        logger.warning(
            "Autodiscovery name collision — skipping",
            rule_id=str(rule.id),
            rule_name=rule.name,
            derived_name=name,
            working_directory=root_directory,
        )
        raise AutodiscoveryNameCollision(name)

    ws = Workspace(
        id=uuid.uuid4(),  # generate_uuid7 default also fine; explicit for log clarity
        name=name,
        execution_mode=rule.execution_mode,
        execution_backend=rule.execution_backend,
        terraform_version=rule.terraform_version,
        resource_cpu=rule.resource_cpu,
        resource_memory=rule.resource_memory,
        auto_apply=rule.auto_apply,
        working_directory=root_directory,
        agent_pool_id=rule.agent_pool_id,
        labels=dict(rule.labels or {}),
        owner_email=rule.owner_email or "",
        vcs_connection_id=rule.vcs_connection_id,
        vcs_repo_url=rule.repo_url,
        vcs_branch=rule.branch,
        autodiscovery_rule_id=rule.id,
        # trigger_prefixes scoped tightly to the discovered directory so
        # the regular VCS poller treats this workspace as a normal
        # working_directory-targeted one from now on.
        trigger_prefixes=[root_directory] if root_directory else [],
    )
    db.add(ws)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Race: another poll cycle / replica autocreated the same
        # workspace between our lookup and flush. Roll back and
        # return the now-existing row.
        await db.rollback()
        existing = await db.execute(
            select(Workspace).where(
                Workspace.vcs_connection_id == rule.vcs_connection_id,
                Workspace.vcs_repo_url == rule.repo_url,
                Workspace.working_directory == root_directory,
            )
        )
        ws = existing.scalar_one_or_none()
        if ws is not None:
            return ws, False
        # Different integrity violation we don't expect — surface it.
        raise exc
    await db.commit()

    logger.info(
        "Autodiscovery created workspace",
        rule_id=str(rule.id),
        rule_name=rule.name,
        workspace_id=str(ws.id),
        workspace_name=ws.name,
        working_directory=root_directory,
        repo_url=rule.repo_url,
    )
    return ws, True


class AutodiscoveryNameCollision(RuntimeError):
    """Raised when the derived workspace name collides with an
    existing unrelated workspace. Operator action required: tighten
    the rule's `name_template` to disambiguate.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"Autodiscovery would create a workspace named {name!r}, but an unrelated workspace with that name already exists"
        )
        self.name = name


# ── Top-level entry point ────────────────────────────────────────────────


async def autodiscover_for_paths(
    db: AsyncSession,
    rules: list[AutodiscoveryRule],
    changed_files: list[str],
) -> list[Workspace]:
    """For a set of changed files, return the list of workspaces that
    should be created/used. New workspaces are persisted; existing
    workspaces are returned untouched.

    Idempotent across repeated calls with the same inputs.
    """
    # Group `(rule, root_directory)` so multiple files in the same
    # directory only fire once.
    matches: dict[tuple[uuid.UUID, str], AutodiscoveryRule] = {}
    for path in changed_files:
        for rule in rules:
            if not rule.enabled:
                continue
            if not rule_claims_path(rule, path):
                continue
            root = derive_root_directory(path)
            matches[(rule.id, root)] = rule
            # First matching rule wins — don't fan out to multiple
            # rules for the same file.
            break

    created: list[Workspace] = []
    for (_rule_id, root), rule in matches.items():
        try:
            ws, _ = await find_or_autocreate_workspace(db, rule, root)
            created.append(ws)
        except AutodiscoveryNameCollision:
            # Logged inside; skip to next match.
            continue
    return created
