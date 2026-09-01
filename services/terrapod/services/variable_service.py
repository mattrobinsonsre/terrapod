"""Variable CRUD and resolution service.

Handles workspace variables, variable sets, and variable resolution
with proper precedence ordering for runner injection.
"""

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    Variable,
    VariableSet,
    VariableSetWorkspace,
    Workspace,
)
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ResolvedVariable:
    """A variable ready for injection into a runner Job."""

    key: str
    value: str
    category: str  # terraform | env | git_http_auth | git_ssh_auth
    structured: bool
    sensitive: bool


def _version_hash(key: str, value: str, category: str) -> str:
    """Compute a content hash for version tracking."""
    content = f"{key}:{value}:{category}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# The categories a variable may carry. `git_http_auth`/`git_ssh_auth` (#1028)
# hold private-git-module credentials — always sensitive, materialized by the
# runner's git_auth phase, never rendered into terraform inputs.
VALID_CATEGORIES = frozenset({"terraform", "env", "git_http_auth", "git_ssh_auth"})
GIT_AUTH_CATEGORIES = frozenset({"git_http_auth", "git_ssh_auth"})


def _validated_category(category: str) -> str:
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"invalid variable category {category!r}; must be one of "
            + ", ".join(sorted(VALID_CATEGORIES))
        )
    return category


async def create_variable(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    key: str,
    value: str,
    category: str = "terraform",
    description: str = "",
    structured: bool = False,
    sensitive: bool = False,
) -> Variable:
    """Create a workspace variable."""
    category = _validated_category(category)
    if category in GIT_AUTH_CATEGORIES:
        sensitive = True  # git-auth values are always secret
    var = Variable(
        workspace_id=workspace_id,
        key=key,
        value=value,
        description=description,
        category=category,
        structured=structured,
        sensitive=sensitive,
        version_id=_version_hash(key, value, category),
    )
    db.add(var)
    await db.flush()
    return var


async def update_variable(
    db: AsyncSession,
    var: Variable,
    key: str | None = None,
    value: str | None = None,
    category: str | None = None,
    description: str | None = None,
    structured: bool | None = None,
    sensitive: bool | None = None,
) -> Variable:
    """Update an existing variable."""
    if key is not None:
        var.key = key
    if description is not None:
        var.description = description
    if category is not None:
        var.category = _validated_category(category)
    if structured is not None:
        var.structured = structured

    was_sensitive = var.sensitive

    if value is not None:
        var.value = value
        var.version_id = _version_hash(var.key, value, var.category)

    # git-auth categories are always secret and can never be downgraded.
    force_sensitive = var.category in GIT_AUTH_CATEGORIES
    if force_sensitive:
        var.sensitive = True
    elif sensitive is not None:
        var.sensitive = sensitive

    # Security: downgrading a variable from sensitive → non-sensitive would
    # otherwise return its previously-hidden value in plaintext (the response
    # masks the value only while `sensitive` is true). A value entered AS a
    # secret must not become world-readable by flipping a flag. If the caller
    # didn't supply a fresh value in the same request, clear it so the old
    # secret is never exposed — the operator must re-enter it.
    if not force_sensitive and was_sensitive and sensitive is False and value is None:
        var.value = ""
        var.version_id = _version_hash(var.key, "", var.category)

    await db.flush()
    return var


async def get_variable(
    db: AsyncSession, workspace_id: uuid.UUID, var_id: uuid.UUID
) -> Variable | None:
    """Get a variable by ID, scoped to workspace."""
    result = await db.execute(
        select(Variable).where(
            Variable.id == var_id,
            Variable.workspace_id == workspace_id,
        )
    )
    return result.scalar_one_or_none()


async def list_variables(db: AsyncSession, workspace_id: uuid.UUID) -> list[Variable]:
    """List all variables for a workspace."""
    result = await db.execute(
        select(Variable).where(Variable.workspace_id == workspace_id).order_by(Variable.key)
    )
    return list(result.scalars().all())


async def delete_variable(db: AsyncSession, var: Variable) -> None:
    """Delete a variable."""
    await db.delete(var)
    await db.flush()


async def resolve_variables(db: AsyncSession, workspace_id: uuid.UUID) -> list[ResolvedVariable]:
    """Resolve all variables for a workspace with proper precedence.

    Precedence (highest wins):
    1. Priority variable set vars (priority=True)
    2. Workspace-level variables
    3. Non-priority variable set vars

    Returns values ready for runner injection.
    """
    resolved: dict[str, ResolvedVariable] = {}

    # Layer 1: Non-priority variable sets (global + assigned)
    varsets = await _get_applicable_varsets(db, workspace_id, priority=False)
    for vs in varsets:
        for vsv in vs.variables:
            resolved[vsv.key] = ResolvedVariable(
                key=vsv.key,
                value=vsv.value,
                category=vsv.category,
                structured=vsv.structured,
                sensitive=vsv.sensitive,
            )

    # Layer 2: Workspace variables (override non-priority sets)
    ws_vars = await list_variables(db, workspace_id)
    for var in ws_vars:
        resolved[var.key] = ResolvedVariable(
            key=var.key,
            value=var.value,
            category=var.category,
            structured=var.structured,
            sensitive=var.sensitive,
        )

    # Layer 3: Priority variable sets (override everything)
    priority_varsets = await _get_applicable_varsets(db, workspace_id, priority=True)
    for vs in priority_varsets:
        for vsv in vs.variables:
            resolved[vsv.key] = ResolvedVariable(
                key=vsv.key,
                value=vsv.value,
                category=vsv.category,
                structured=vsv.structured,
                sensitive=vsv.sensitive,
            )

    return list(resolved.values())


#: How a variable set came to apply to a workspace. Surfaced to the UI so an
#: operator can tell what they may edit — an explicit assignment is theirs to
#: remove, a global or rule-derived one is not (#1440).
ASSIGNMENT_EXPLICIT = "explicit"
ASSIGNMENT_GLOBAL = "global"
ASSIGNMENT_RULE = "rule"


async def applicable_varsets(
    db: AsyncSession, workspace_id: uuid.UUID, priority: bool | None = None
) -> list[tuple[VariableSet, str]]:
    """Variable sets applying to a workspace, each with how it came to apply.

    The single source of truth for that question. Both variable *resolution* and
    the read-only association views go through here, so what an operator is shown
    cannot drift from what is actually injected — a UI that confidently lists a
    different set of workspaces from the one receiving a credential is worse than
    no UI, because it will be believed.

    `priority=None` returns both tiers, which is what the association views want;
    resolution asks for one tier at a time because precedence differs.
    """
    seen: dict[uuid.UUID, tuple[VariableSet, str]] = {}

    def _tier(q):
        return q if priority is None else q.where(VariableSet.priority.is_(priority))

    # Explicit assignment first: it is the strongest claim and the only one an
    # operator can act on directly, so it wins the label if a set also matches a
    # rule.
    assigned = await db.execute(
        _tier(
            select(VariableSet)
            .join(VariableSetWorkspace, VariableSet.id == VariableSetWorkspace.variable_set_id)
            .where(
                VariableSetWorkspace.workspace_id == workspace_id,
                VariableSet.global_set.is_(False),
            )
        )
    )
    for vs in assigned.scalars().all():
        seen[vs.id] = (vs, ASSIGNMENT_EXPLICIT)

    global_sets = await db.execute(
        _tier(select(VariableSet).where(VariableSet.global_set.is_(True)))
    )
    for vs in global_sets.scalars().all():
        seen.setdefault(vs.id, (vs, ASSIGNMENT_GLOBAL))

    # Rule-based (#1440). Each rule is evaluated by handing it back to the same
    # SQL selector the bulk-update surface uses, narrowed to this one workspace.
    # A second matcher written in Python would drift from that one, and the two
    # disagreeing about who receives a credential is the failure worth avoiding.
    ruled = await db.execute(
        _tier(
            select(VariableSet).where(
                VariableSet.global_set.is_(False),
                VariableSet.assignment_rule.is_not(None),
            )
        )
    )
    for vs in ruled.scalars().all():
        if vs.id in seen:
            continue
        if await _rule_matches(db, vs.assignment_rule, workspace_id):
            seen[vs.id] = (vs, ASSIGNMENT_RULE)

    out = list(seen.values())
    for vs, _ in out:
        await db.refresh(vs, ["variables"])
    return out


async def workspaces_for_varset(
    db: AsyncSession, varset: VariableSet
) -> list[tuple[Workspace, str]]:
    """Workspaces this variable set applies to, and how each came to apply.

    The inverse of :func:`applicable_varsets`, and the blast-radius view: for a
    set carrying a credential, "who currently receives this" must be answerable
    without reading the rule and simulating it by hand.

    Global sets apply everywhere, so the answer is every workspace — reported
    honestly rather than as an empty list, because "applies to nothing" would be
    the opposite of the truth.
    """
    from terrapod.services import workspace_search_service as wss

    if varset.global_set:
        rows = await db.execute(select(Workspace).order_by(Workspace.name))
        return [(ws, ASSIGNMENT_GLOBAL) for ws in rows.scalars().all()]

    seen: dict[uuid.UUID, tuple[Workspace, str]] = {}

    explicit = await db.execute(
        select(Workspace)
        .join(VariableSetWorkspace, Workspace.id == VariableSetWorkspace.workspace_id)
        .where(VariableSetWorkspace.variable_set_id == varset.id)
        .order_by(Workspace.name)
    )
    for ws in explicit.scalars().all():
        seen[ws.id] = (ws, ASSIGNMENT_EXPLICIT)

    if varset.assignment_rule:
        try:
            parsed = wss.parse_filter(varset.assignment_rule)
        except Exception:
            logger.warning(
                "variable set has an unparseable assignment rule; matching nothing",
                varset_id=str(varset.id),
            )
        else:
            matched = await db.execute(wss.build_workspace_query(parsed).order_by(Workspace.name))
            for ws in matched.scalars().all():
                seen.setdefault(ws.id, (ws, ASSIGNMENT_RULE))

    return sorted(seen.values(), key=lambda row: row[0].name)


async def _rule_matches(db: AsyncSession, rule: dict | None, workspace_id: uuid.UUID) -> bool:
    """Whether a workspace satisfies an assignment rule.

    A malformed rule matches nothing rather than everything. A stored rule that
    no longer parses — a filter key removed in some later version, say — must not
    silently become "all workspaces" and hand a credential to the estate.
    """
    from terrapod.services import workspace_search_service as wss

    if not rule:
        return False
    try:
        parsed = wss.parse_filter(rule)
    except Exception:
        logger.warning("variable set has an unparseable assignment rule; matching nothing")
        return False

    q = wss.build_workspace_query(parsed).where(Workspace.id == workspace_id)
    found = await db.execute(select(q.exists()))
    return bool(found.scalar())


async def _get_applicable_varsets(
    db: AsyncSession, workspace_id: uuid.UUID, priority: bool
) -> list[VariableSet]:
    """Variable sets applicable to a workspace, for one precedence tier."""
    return [vs for vs, _ in await applicable_varsets(db, workspace_id, priority=priority)]
