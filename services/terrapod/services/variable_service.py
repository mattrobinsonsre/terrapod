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
)
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class ResolvedVariable:
    """A variable ready for injection into a runner Job."""

    key: str
    value: str
    category: str  # "terraform" or "env"
    hcl: bool
    sensitive: bool


def _version_hash(key: str, value: str, category: str) -> str:
    """Compute a content hash for version tracking."""
    content = f"{key}:{value}:{category}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


async def create_variable(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    key: str,
    value: str,
    category: str = "terraform",
    description: str = "",
    hcl: bool = False,
    sensitive: bool = False,
) -> Variable:
    """Create a workspace variable."""
    var = Variable(
        workspace_id=workspace_id,
        key=key,
        value=value,
        description=description,
        category=category,
        hcl=hcl,
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
    hcl: bool | None = None,
    sensitive: bool | None = None,
) -> Variable:
    """Update an existing variable."""
    if key is not None:
        var.key = key
    if description is not None:
        var.description = description
    if category is not None:
        var.category = category
    if hcl is not None:
        var.hcl = hcl

    if value is not None:
        var.value = value
        var.version_id = _version_hash(var.key, value, var.category)

    if sensitive is not None:
        var.sensitive = sensitive

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
                hcl=vsv.hcl,
                sensitive=vsv.sensitive,
            )

    # Layer 2: Workspace variables (override non-priority sets)
    ws_vars = await list_variables(db, workspace_id)
    for var in ws_vars:
        resolved[var.key] = ResolvedVariable(
            key=var.key,
            value=var.value,
            category=var.category,
            hcl=var.hcl,
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
                hcl=vsv.hcl,
                sensitive=vsv.sensitive,
            )

    return list(resolved.values())


async def _get_applicable_varsets(
    db: AsyncSession, workspace_id: uuid.UUID, priority: bool
) -> list[VariableSet]:
    """Get variable sets applicable to a workspace."""
    # Global sets
    global_q = select(VariableSet).where(
        VariableSet.global_set.is_(True),
        VariableSet.priority.is_(priority),
    )

    # Assigned sets
    assigned_q = (
        select(VariableSet)
        .join(VariableSetWorkspace, VariableSet.id == VariableSetWorkspace.variable_set_id)
        .where(
            VariableSetWorkspace.workspace_id == workspace_id,
            VariableSet.global_set.is_(False),
            VariableSet.priority.is_(priority),
        )
    )

    global_result = await db.execute(global_q)
    assigned_result = await db.execute(assigned_q)

    varsets = list(global_result.scalars().all()) + list(assigned_result.scalars().all())

    # Eagerly load variables for each set
    for vs in varsets:
        await db.refresh(vs, ["variables"])

    return varsets
