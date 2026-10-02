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


#: Workspace attributes that provably cannot change whether an assignment rule
#: matches. This is a DENYLIST, which is the whole point: the check fails OPEN, so
#: an attribute nobody has classified still pays for the guard. The first version
#: of this was an allowlist of *triggering* keys and therefore failed closed-to-skip
#: — a new attribute silently escaped the check, and the docstring claimed the
#: opposite. Both reviewers found it.
NOT_RULE_SELECTABLE: frozenset[str] = frozenset(
    {
        "description",
        "auto-apply",
        "auto-apply-mode",
        "queue-all-runs",
        "speculative-enabled",
        "allow-destroy-plan",
        "terraform-working-directory",
        "working-directory",
        "trigger-prefixes",
        "file-triggers-enabled",
        "slack-channel",
        "ai-summary-mode",
        "ai-summary-context",
        "ai-policy-mode",
        "notifications",
        "resource-cpu",
        "resource-memory",
        "debug-mode",
        "allow-fork-pr-plans",
        "vcs-branch",
        "vcs-workflow",
        "auto-merge",
        "auto-merge-strategy",
        "drift-detection-interval-seconds",
    }
)

#: Rule dimensions a workspace's own owner must not be able to move, so they are
#: refused as assignment-rule selectors rather than guarded at every write site.
#:
#: `drift_status` and `locked` are platform state, not identity, and both are
#: writable by endpoints that have no business paying a variable-set check:
#: `POST .../actions/dismiss-drift` needs only `drift:dismiss`, `PATCH
#: {"drift-detection-enabled": false}` clears drift status as a side effect, and
#: lock/unlock/force-unlock need only `workspace:lock`. A rule keyed on either was
#: therefore self-joinable through a door the guard does not watch — five doors.
#:
#: Gating all five would work and would be worse: scoping a credential on "is this
#: workspace currently drifted" is not a thing anyone should be able to express, so
#: the dimension is the defect rather than the missing gate.
RULE_DIMENSIONS_REFUSED: dict[str, str] = {
    "drift_status": (
        "drift status is written by the drift checker and cleared by "
        "`dismiss-drift` and by disabling drift detection, so a workspace's own "
        "owner can move it"
    ),
    "locked": (
        "lock state is moved by the lock, unlock and force-unlock endpoints, which "
        "need only `workspace:lock`"
    ),
}


def rule_refused_dimensions(rule) -> list[str]:
    """The refused dimensions this rule selects on, sorted. Empty means none.

    Every consumer of `assignment_rule` must agree about this, and they did not: the
    matcher that decides delivery refused these dimensions while the blast-radius view
    that answers "who currently receives this credential" did not, so the view listed
    workspaces that had already stopped receiving the set. On a rule keyed only on a
    refused dimension the matcher matches nothing at all, so the view reported reach
    where there was none — and it is the screen an operator reads before rotating a
    credential.

    One predicate rather than the same comprehension in three files, with
    `test_every_assignment_rule_consumer_refuses_the_same_dimensions` failing if a
    fourth consumer appears without it.
    """
    if not isinstance(rule, dict):
        return []
    return sorted(k for k in RULE_DIMENSIONS_REFUSED if k in rule)


def touches_rule_selectable(attrs: dict, relationships: dict | None = None) -> bool:
    """Whether this request body could change which assignment rules match.

    Fails OPEN by construction: it asks whether EVERY key in the body is known not
    to matter, so an attribute nobody has classified counts as touching. The point
    is only to spare the three queries on a body that provably cannot move the
    answer — a description edit, a notification toggle.

    Getting this the wrong way round is the subtle version of switching the guard
    off, which is what the first version did.
    """
    if relationships:
        # Any relationship may carry a connection or a pool; neither is worth
        # enumerating against a body shape that can nest.
        return True
    if not attrs:
        return False
    return any(k not in NOT_RULE_SELECTABLE for k in attrs)


async def rule_assigned_varset_ids(db: AsyncSession, workspace_id: uuid.UUID) -> set[uuid.UUID]:
    """The variable sets reaching this workspace *by assignment rule*.

    Asked through `applicable_varsets`, which is the single source of truth for
    that question and the same path resolution takes, so this cannot drift from
    what is actually injected into a run.
    """
    from terrapod.services.variable_service import ASSIGNMENT_RULE, applicable_varsets

    applicable = await applicable_varsets(db, workspace_id)
    return {vs.id for vs, how in applicable if how == ASSIGNMENT_RULE}


async def _sets_holding_secrets(db: AsyncSession, set_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Of these variable sets, the ones carrying a secret.

    `sensitive` is the stored-secret case. `value_source` is the brokered one — an
    OpenBao/Vault reference resolved at run time, whose value never sits in the
    column at all, so a check that only looked at `sensitive` would wave through
    precisely the sets whose contents are most worth having.

    **It is compared against `"static"`, not against NULL.** The column is NOT NULL
    and defaults to `"static"`, so `value_source IS NOT NULL` matches every variable
    ever written — which would have made this whole narrowing a silent no-op while
    looking exactly like a narrowing. Checked against the model rather than assumed.

    The `value_source` clause is **not load-bearing today**: the variables router
    stores a vault-sourced variable with `sensitive` forced true, so the first clause
    already catches everything written through the API. It stays as defence for rows
    this router did not write — a migration, a direct fixup, a future writer that
    sets the source without the flag. Removing it fails no test, and the test says
    so rather than implying coverage it does not have.
    """
    from sqlalchemy import or_, select

    from terrapod.db.models import VariableSetVariable

    if not set_ids:
        return set()
    rows = await db.execute(
        select(VariableSetVariable.variable_set_id)
        .where(
            VariableSetVariable.variable_set_id.in_(set_ids),
            or_(
                VariableSetVariable.sensitive.is_(True),
                VariableSetVariable.value_source != "static",
            ),
        )
        .distinct()
    )
    return {r for (r,) in rows.all()}


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

    # Narrow the refusal to sets that actually hold something worth taking.
    #
    # Refusing EVERY match closes the finding and also closes the feature: the
    # documented workflow is an admin writing "label `env=prod` -> varset
    # `aws-prod-creds`" and developers self-servicing `env=prod` workspaces, and the
    # service catalog is *entirely* non-admin self-service, so a blanket refusal
    # breaks every catalog item whose labels match a rule. The reported impact is
    # specific — "variable-set credentials, including sensitive static values and
    # Vault-brokered secrets" — so that is what is refused: a set carrying a
    # `sensitive` variable or one resolved through a broker. A rule-assigned set of
    # plain configuration joining automatically is the feature working.
    #
    # A guard that refuses ordinary work gets switched off, which would leave the
    # secrets unprotected too.
    gained = await _sets_holding_secrets(db, gained)
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
