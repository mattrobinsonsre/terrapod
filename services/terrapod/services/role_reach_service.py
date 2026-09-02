"""Role reach — which workspaces a role grants on, and why (#1456).

Label-based RBAC scales as a mechanism but not as something a person can
verify. At a few hundred workspaces you can eyeball who ``env:prod`` reaches;
at ten thousand you cannot, so operators either over-grant (a broad rule being
the only one they can reason about) or avoid deny rules entirely, because the
allow/deny interaction is where mistakes hide and nothing shows the outcome
before saving. This service answers the question directly.

Two properties matter more than the feature itself:

**It resolves through the enforcement path.** Per-workspace verdicts come from
``capability_resolver.role_match_verdict`` — the same gate authorization uses,
returning the reason alongside the answer. Nothing here re-implements label
matching. A permissions view that disagrees with enforcement is worse than no
view, and a parallel matcher is free to drift the moment either side changes.

**It does not resolve the fleet in Python.** The allow and deny rules are both
expressible as SQL (name membership, and JSONB containment per label pair), so
counts are two aggregate queries and the caller only ever materialises one
page. The workspace list was O(N)-per-request once already (#1056) — every
workspace loaded, capabilities resolved in an await loop, then sliced to a
page. This is that bug's natural shape, and avoiding it is a design
requirement, not an optimisation.

Scope: role reach is a property of the ROLE, not of any user holding it.
Platform-admin, platform-audit and workspace-ownership are per-principal paths
that grant regardless of the role, so they are reported per workspace as notes
rather than folded into the capability set — telling an operator "this role
grants read here" while the viewer is the owner with admin would be true and
useless.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, Select, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth import capabilities as cap
from terrapod.db.models import Role, Workspace
from terrapod.services.capability_resolver import (
    MATCH_ALLOWED,
    role_effective_capabilities,
    role_match_verdict,
)

#: Per-workspace notes. Each names a path to access that exists INDEPENDENTLY of
#: the role being previewed, so an operator reading the result is not misled
#: into thinking the role is the only thing granting here.
NOTE_CATALOG_CLAMPED = "catalog-clamped"
NOTE_EVERYONE_FLOOR = "everyone-floor"
NOTE_HAS_OWNER = "has-owner"


def _label_pairs(rule: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Flatten a role's label rule into (key, value) pairs.

    A rule value may be a scalar or a list — ``merge_labels`` accepts both, so
    this must too, or a role written the list way silently matches nothing.
    """
    pairs: list[tuple[str, str]] = []
    for key, values in (rule or {}).items():
        if isinstance(values, list):
            pairs.extend((str(key), str(v)) for v in values)
        else:
            pairs.append((str(key), str(values)))
    return pairs


def _rule_clause(names: list[str] | None, labels: dict | None) -> ColumnElement[bool] | None:
    """SQL for one side of a role's rules (allow or deny), or None when that
    side is empty — an empty rule set matches nothing, which is not the same as
    matching everything, and conflating the two would silently widen the answer.
    """
    clauses: list[ColumnElement[bool]] = []
    if names:
        clauses.append(Workspace.name.in_(list(names)))
    for key, value in _label_pairs(labels):
        # JSONB containment, so the match happens in the index rather than by
        # loading every workspace and comparing in Python.
        clauses.append(Workspace.labels.contains({key: value}))
    if not clauses:
        return None
    return or_(*clauses)


def granted_query(role: Role) -> Select[tuple[Workspace]]:
    """Workspaces this role reaches: allow-matched and not denied."""
    allow = _rule_clause(role.allow_names, role.allow_labels)
    if allow is None:
        # No allow rules: the role reaches nothing by label RBAC. Returning a
        # match-nothing query keeps every caller on one code path.
        return select(Workspace).where(false())
    q = select(Workspace).where(allow)
    deny = _rule_clause(role.deny_names, role.deny_labels)
    if deny is not None:
        q = q.where(~deny)
    return q.order_by(Workspace.name)


def denied_query(role: Role) -> Select[tuple[Workspace]]:
    """Workspaces this role would have reached but for a deny rule.

    Reported separately rather than simply omitted: "matched 47, denied 3" is
    the feedback that makes a deny rule safe to write, and an operator who
    cannot see what a deny removed cannot tell an intended exclusion from a
    typo.
    """
    allow = _rule_clause(role.allow_names, role.allow_labels)
    deny = _rule_clause(role.deny_names, role.deny_labels)
    if allow is None or deny is None:
        return select(Workspace).where(false())
    return select(Workspace).where(allow, deny).order_by(Workspace.name)


def _workspace_entry(role: Role, ws: Workspace) -> dict[str, Any]:
    """One workspace's verdict, resolved capabilities and notes."""
    verdict, reason = role_match_verdict(role, ws.name, ws.labels or {})
    caps: frozenset[str] = frozenset()
    notes: list[str] = []
    if verdict == MATCH_ALLOWED:
        caps = role_effective_capabilities(role) & cap.axis_all_caps("workspace")
        # The catalog clamp is a property of the workspace, not the role: a
        # catalog-managed workspace caps every non-platform-admin grant at read,
        # so a role granting write here does not actually give write.
        if ws.catalog_item_id is not None:
            clamped = caps & cap.axis_read_caps("workspace")
            if clamped != caps:
                notes.append(NOTE_CATALOG_CLAMPED)
            caps = clamped
    if (ws.labels or {}).get("access") == "everyone":
        notes.append(NOTE_EVERYONE_FLOOR)
    if ws.owner_email:
        notes.append(NOTE_HAS_OWNER)
    return {
        "id": f"ws-{ws.id}",
        "name": ws.name,
        "labels": ws.labels or {},
        "owner-email": ws.owner_email or "",
        "verdict": verdict,
        "reason": reason,
        "capabilities": sorted(caps),
        "notes": notes,
    }


async def preview_role_reach(
    db: AsyncSession,
    role: Role,
    *,
    limit: int = 25,
    offset: int = 0,
    include_denied: bool = True,
) -> dict[str, Any]:
    """Which workspaces this role grants on, with counts over the whole fleet
    and one page of detail.

    The counts are aggregates so they stay honest at fleet scale — an operator
    needs "this rule reaches 4,200 workspaces" to be true, not truncated to
    whatever the page happened to hold.
    """
    granted = granted_query(role)
    denied = denied_query(role)

    granted_total = await db.scalar(select(func.count()).select_from(granted.subquery())) or 0
    denied_total = await db.scalar(select(func.count()).select_from(denied.subquery())) or 0

    rows = (await db.execute(granted.limit(limit).offset(offset))).scalars().all()
    entries = [_workspace_entry(role, ws) for ws in rows]

    denied_entries: list[dict[str, Any]] = []
    if include_denied and denied_total:
        # Bounded deliberately: the denied set is an explanation, not a listing
        # to page through. A large one means the deny rule is doing a lot, which
        # the count already says more usefully than a thousand rows would.
        denied_rows = (await db.execute(denied.limit(limit))).scalars().all()
        denied_entries = [_workspace_entry(role, ws) for ws in denied_rows]

    return {
        "granted-count": granted_total,
        "denied-count": denied_total,
        "matched-count": granted_total + denied_total,
        "workspaces": entries,
        "denied": denied_entries,
        "denied-truncated": denied_total > len(denied_entries),
    }
