"""A workspace cannot be shaped to pull in a variable set (GHSA-49q6-pm68-3xgw).

A variable set's assignment rule selects workspaces by labels, name, execution
mode, agent pool, engine version, VCS connection, drift status and so on — and
every one of those except `owner_email` is settable by the workspace's own owner.
Workspace creation is open and the creator becomes owner, so any authenticated user
could label a workspace into another team's rule-assigned set and have its
variables — including sensitive values and OpenBao/Vault-brokered secrets —
delivered into a run they control. Confirmed end to end by the reporter.

The hole is that there is no entitlement to check. A variable set is admin-managed
and has no RBAC of its own, so "may this principal receive this set" has no answer
to look up. What there IS is a before and an after: the set of rule-assigned
variable sets reaching this workspace, recomputed with the change applied. A
non-admin may shrink it or leave it alone; growing it is the escalation, so growth
is refused.

Three properties worth keeping in mind, because each is a way to get this wrong:

- **Only RULE-assigned sets count.** A globally-scoped set already reaches every
  workspace and an explicitly-assigned one was an admin's deliberate act on this
  workspace. Counting either would refuse ordinary edits for no gain.
- **It is evaluated against the pending row, not the request.** Re-deriving "would
  this rule match" from the submitted attributes would be a second matcher, and two
  matchers disagreeing about who receives a credential is the failure this guards.
  So the change is flushed and the real selector is asked.
- **Shrinking is allowed.** Dropping a label that was pulling a set in is a
  de-escalation and must not need an admin.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.logging_config import get_logger

logger = get_logger(__name__)


#: PATCH body attribute keys that can change whether an assignment rule matches.
#: Keyed to `WorkspaceFilter`'s dimensions, and `test_every_filter_dimension_is_
#: classified` fails if that model grows one nothing here accounts for — so the
#: set cannot silently stop covering a dimension, which is the way a guard like
#: this rots.
RULE_SELECTABLE_ATTRS: frozenset[str] = frozenset(
    {
        "labels",
        "name",
        "execution-backend",
        "execution-mode",
        "terraform-version",
        "engine-version",
        "agent-pool-id",
        "agent-pool-ids",
        "vcs-connection-id",
        "vcs-repo-url",
        "owner-email",
    }
)

#: `WorkspaceFilter` dimensions that no PATCH attribute can move, with the reason.
#: An entry here is a claim that the dimension cannot be self-assigned, which is
#: the only basis on which it is safe to leave out of the check above.
FILTER_DIMENSIONS_NOT_PATCHABLE: dict[str, str] = {
    "workspace_ids": "the id is assigned at create and immutable",
    "drift_status": "written by the drift checker, not by a request",
    "locked": "moved by the lock/unlock endpoints, which take no attributes",
    "has_vcs": "derived from vcs-connection-id and vcs-repo-url, both watched above",
    "name_prefix": "selects on `name`, which is watched above",
    "name_glob": "selects on `name`, which is watched above",
    "all": "not a workspace attribute — it is the filter's explicit select-everything flag",
}


def touches_rule_selectable(attrs: dict, relationships: dict | None = None) -> bool:
    """Whether this request body could change which assignment rules match.

    The point is to avoid three queries on every workspace PATCH that cannot
    possibly move the answer — a rename of an unrelated field, a description edit,
    a notification toggle. It fails OPEN: anything unrecognised counts as touching,
    so a new attribute is covered before anyone remembers to classify it.
    """
    if any(k in attrs for k in RULE_SELECTABLE_ATTRS):
        return True
    # The connection also arrives as a relationship, and gating on the attribute
    # spelling alone is how the PATCH gate was got wrong once already.
    return bool(relationships and "vcs-connection" in relationships)


async def rule_assigned_varset_ids(db: AsyncSession, workspace_id: uuid.UUID) -> set[uuid.UUID]:
    """The variable sets reaching this workspace *by assignment rule*.

    Asked through `applicable_varsets`, which is the single source of truth for
    that question and the same path resolution takes, so this cannot drift from
    what is actually injected into a run.
    """
    from terrapod.services.variable_service import ASSIGNMENT_RULE, applicable_varsets

    applicable = await applicable_varsets(db, workspace_id)
    return {vs.id for vs, how in applicable if how == ASSIGNMENT_RULE}


async def refuse_varset_growth(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    before: set[uuid.UUID],
    is_platform_admin: bool,
    actor_email: str,
) -> None:
    """Raise 403 if the pending change pulls in a rule-assigned set.

    Call AFTER flushing the change and BEFORE committing it. A platform admin is
    exempt: they can already read every variable set, so there is nothing for them
    to escalate to.

    The caller rolls back — this raises rather than rolling back itself so the
    router keeps control of its own transaction, which is the same reason the
    gate in `complete_plan` does not roll back the caller's work.
    """
    if is_platform_admin:
        return

    after = await rule_assigned_varset_ids(db, workspace_id)
    gained = after - before
    if not gained:
        return

    # Name them: an operator who hits this needs to know which set to ask about,
    # and the names are not secret — the values are.
    from sqlalchemy import select

    from terrapod.db.models import VariableSet

    rows = await db.execute(select(VariableSet.name).where(VariableSet.id.in_(gained)))
    names = sorted(n for (n,) in rows.all())

    logger.warning(
        "refused a workspace change that would pull in a rule-assigned variable set",
        workspace_id=str(workspace_id),
        actor=actor_email,
        gained=names,
    )
    raise HTTPException(
        status_code=403,
        detail=(
            "This change would make the workspace match the assignment rule of "
            f"{'variable set' if len(names) == 1 else 'variable sets'} "
            f"{', '.join(repr(n) for n in names)}, whose variables would then be "
            "delivered into its runs. A variable set is admin-managed and has no "
            "per-set permissions, so there is nothing to check a claim against and "
            "the match itself would be the grant (GHSA-49q6-pm68-3xgw). Ask a "
            "platform admin to make the change, or to assign the set to this "
            "workspace explicitly."
        ),
    )
