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

from sqlalchemy import ColumnElement, Select, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth import capabilities as cap
from terrapod.db.models import (
    AgentPool,
    CatalogItem,
    RegistryModule,
    RegistryProvider,
    Role,
    RoleAssignment,
    Workspace,
)
from terrapod.services.capability_resolver import (
    MATCH_ALLOWED,
    MATCH_NONE,
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


def _rule_clause(model, names: list[str] | None, labels: dict | None) -> ColumnElement[bool] | None:
    """SQL for one side of a role's rules (allow or deny) against `model`, or
    None when that side is empty.

    An empty rule set matches NOTHING, which is not the same as matching
    everything — conflating the two would silently report a role as reaching
    the whole estate.
    """
    clauses: list[ColumnElement[bool]] = []
    if names:
        clauses.append(model.name.in_(list(names)))
    for key, value in _label_pairs(labels):
        # JSONB containment, so the match happens in the index rather than by
        # loading every row and comparing in Python.
        clauses.append(model.labels.contains({key: value}))
    if not clauses:
        return None
    return or_(*clauses)


#: The resource kinds a role's rules reach, and the capability axis each is
#: judged on. `_role_matches` is axis-agnostic — it compares a name and labels —
#: so the SAME allow/deny rules govern every one of these. A preview that
#: covered only workspaces would therefore be answering a quarter of the
#: question while looking complete, which is worse than obviously partial.
#:
#: `registry` deliberately spans two models: modules and providers share one
#: capability axis, so they share one answer.
AXIS_MODELS: dict[str, list[tuple[str, Any]]] = {
    "workspace": [("workspaces", Workspace)],
    "pool": [("agent-pools", AgentPool)],
    "registry": [("registry-modules", RegistryModule), ("registry-providers", RegistryProvider)],
    "catalog": [("catalog-items", CatalogItem)],
}


def _allow_clause(role: Role, model) -> ColumnElement[bool] | None:
    """The allow side as SQL. `allow_all` matches every row; otherwise the
    name/label rules do, exactly (there are no wildcards — see
    `first_matching_label`; if globbing is ever added to role rules, THIS is
    the place that would silently under-match)."""
    if role.allow_all:
        return true()
    return _rule_clause(model, role.allow_names, role.allow_labels)


def granted_query(role: Role, model) -> Select:
    """Resources of `model` this role reaches: allow-matched and not denied."""
    allow = _allow_clause(role, model)
    if allow is None:
        # No allow rules: the role reaches nothing by label RBAC. A
        # match-nothing query keeps every caller on one code path.
        return select(model).where(false())
    q = select(model).where(allow)
    deny = _rule_clause(model, role.deny_names, role.deny_labels)
    if deny is not None:
        q = q.where(~deny)
    return q.order_by(model.name)


def denied_query(role: Role, model) -> Select:
    """Resources this role would have reached but for a deny rule.

    Reported rather than simply omitted: "matched 47, denied 3" is the feedback
    that makes a deny rule safe to write, and someone who cannot see what a deny
    removed cannot tell an intended exclusion from a typo.
    """
    allow = _allow_clause(role, model)
    deny = _rule_clause(model, role.deny_names, role.deny_labels)
    if allow is None or deny is None:
        return select(model).where(false())
    return select(model).where(allow, deny).order_by(model.name)


def _entry(role: Role, axis: str, kind: str, obj: Any) -> dict[str, Any]:
    """One resource's verdict, resolved capabilities and notes.

    Capabilities are sliced to the axis being judged, so a role granting
    `registry:admin` and `workspace:read` reports admin against a module and
    read against a workspace — rather than one undifferentiated set that is
    wrong on both.
    """
    verdict, reason = role_match_verdict(role, obj.name, obj.labels or {})
    caps: frozenset[str] = frozenset()
    notes: list[str] = []
    if verdict == MATCH_ALLOWED:
        caps = role_effective_capabilities(role) & cap.axis_all_caps(axis)
        # The catalog clamp is a property of the WORKSPACE, not of the role: a
        # catalog-managed workspace caps every non-platform-admin grant at read,
        # so a role granting write there does not actually give write.
        if axis == "workspace" and getattr(obj, "catalog_item_id", None) is not None:
            clamped = caps & cap.axis_read_caps(axis)
            if clamped != caps:
                notes.append(NOTE_CATALOG_CLAMPED)
            caps = clamped
    # The everyone-floor does not apply to the catalog axis (it is opt-in), so
    # claiming it there would be a lie.
    if axis != "catalog" and (obj.labels or {}).get("access") == "everyone":
        notes.append(NOTE_EVERYONE_FLOOR)
    if getattr(obj, "owner_email", ""):
        notes.append(NOTE_HAS_OWNER)
    return {
        "id": f"{_ID_PREFIX.get(kind, '')}{obj.id}",
        "kind": kind,
        "name": obj.name,
        "labels": obj.labels or {},
        "owner-email": getattr(obj, "owner_email", "") or "",
        "verdict": verdict,
        "reason": reason,
        "capabilities": sorted(caps),
        "notes": notes,
    }


#: Typed id prefixes, matching what each resource's own endpoints emit.
_ID_PREFIX = {
    "workspaces": "ws-",
    "agent-pools": "apool-",
    "registry-modules": "",
    "registry-providers": "",
    "catalog-items": "",
}


async def _reach_for_axis(
    db: AsyncSession, role: Role, axis: str, *, limit: int, offset: int, include_denied: bool
) -> dict[str, Any]:
    """Counts over the whole estate for one axis, plus a page of detail."""
    granted_total = 0
    denied_total = 0
    entries: list[dict[str, Any]] = []
    denied_entries: list[dict[str, Any]] = []

    for kind, model in AXIS_MODELS[axis]:
        g = granted_query(role, model)
        d = denied_query(role, model)
        granted_total += await db.scalar(select(func.count()).select_from(g.subquery())) or 0
        denied_total += await db.scalar(select(func.count()).select_from(d.subquery())) or 0
        # Fetch offset+limit from EACH model rather than applying the offset per
        # model: the registry axis spans two (modules and providers), and paging
        # them independently returned up to 2x the page size while skipping rows
        # — page 2 of 25 gave modules 26-50 AND providers 26-50 against a count
        # that described the union. The first (offset+limit) rows of the union
        # are always contained in the union of each model's first
        # (offset+limit), so this window is sufficient; it is sorted and sliced
        # below.
        window = offset + limit
        for obj in (await db.execute(g.limit(window))).scalars().all():
            entries.append(_entry(role, axis, kind, obj))
        if include_denied:
            for obj in (await db.execute(d.limit(window))).scalars().all():
                denied_entries.append(_entry(role, axis, kind, obj))

    # Order across the whole axis, then take the page. Single-model axes are
    # unaffected (already ordered by name in SQL); multi-model ones need it.
    entries.sort(key=lambda e: (e["kind"], e["name"]))
    denied_entries.sort(key=lambda e: (e["kind"], e["name"]))
    entries = entries[offset : offset + limit]
    denied_entries = denied_entries[:limit]

    return {
        "granted-count": granted_total,
        "denied-count": denied_total,
        "matched-count": granted_total + denied_total,
        "resources": entries,
        "denied": denied_entries,
        "denied-truncated": denied_total > len(denied_entries),
    }


class ViewerNotPermitted(PermissionError):
    """The caller may not be shown an unfiltered estate-wide answer."""


def assert_viewer_sees_everything(viewer_roles: list[str] | None) -> None:
    """Refuse to build an estate-wide answer for a caller who cannot already
    see the estate.

    Both readers of this service list EVERY matching resource by name, with its
    labels and owner, across every axis — regardless of what the CALLER can
    see. That is safe today only because the endpoints are gated on platform
    `admin` or `audit`, and both already resolve to full visibility (admin →
    all capabilities on every axis; audit → the read floor on every axis).

    The safety therefore rests on the gate, not on anything this code does —
    and the gate is exactly the thing a future change is likely to loosen, so
    that a team lead can author a role scoped to their own labels. If that
    happens without per-viewer filtering being written first, this becomes an
    estate-wide disclosure to someone entitled to a corner of it.

    So the coupling is made explicit and fails CLOSED: loosening the gate
    without doing the filtering work raises here instead of quietly leaking.
    Implementing the filter means resolving the viewer's capabilities per
    resource and reporting visible-only counts — deliberately NOT done blind,
    because it reintroduces the per-request O(fleet) resolution that #1056
    removed, and it must be designed rather than bolted on.
    """
    # NOTE: callers must pass the EFFECTIVE platform roles
    # (`dependencies.effective_platform_roles(user)`), not `user.roles`. A
    # service-token principal's raw list is wider than what the gate honours —
    # `service_bound` is live ∩ pinned and `service_detached` is pinned only —
    # so checking the raw list would make this backstop inert for exactly the
    # token kinds attenuation exists for.
    if not {"admin", "audit"} & set(viewer_roles or []):
        raise ViewerNotPermitted(
            "role reach is reported across the whole estate, so it is offered only to "
            "platform admin/audit, who can already see it. Showing it to a narrower "
            "principal needs per-viewer filtering (and visible-only counts) first."
        )


async def preview_role_reach(
    db: AsyncSession,
    role: Role,
    *,
    limit: int = 25,
    offset: int = 0,
    include_denied: bool = True,
    viewer_roles: list[str] | None = None,
) -> dict[str, Any]:
    """What this role reaches, across EVERY axis its rules govern.

    A role's allow/deny rules are matched the same way whatever they are
    matched against, so the same rule that selects workspaces also selects
    agent pools, registry modules and providers, and catalog items. Reporting
    only workspaces would answer a quarter of the question while looking
    complete.

    The three top-level counts (`granted-count` / `denied-count` /
    `matched-count`) are CROSS-AXIS totals — summed over workspaces, pools,
    registry and catalog — and over the whole estate, not the returned page.
    The per-axis figures live under `axes`. `workspaces` / `denied` /
    `denied-truncated` at the top level are the WORKSPACE axis promoted for
    convenience (what the editor leads with); they are NOT the denominator of
    the cross-axis counts. A consumer wanting the workspace count reads
    `axes["workspace"]["granted-count"]`.
    """
    assert_viewer_sees_everything(viewer_roles)
    axes = {
        axis: await _reach_for_axis(
            db, role, axis, limit=limit, offset=offset, include_denied=include_denied
        )
        for axis in AXIS_MODELS
    }
    return {
        "granted-count": sum(a["granted-count"] for a in axes.values()),
        "denied-count": sum(a["denied-count"] for a in axes.values()),
        "matched-count": sum(a["matched-count"] for a in axes.values()),
        "axes": axes,
        # The workspace axis promoted to the top level: it is what the role
        # editor leads with, and by far the most asked-about.
        "workspaces": axes["workspace"]["resources"],
        "denied": axes["workspace"]["denied"],
        # Promoted with them, or a caller reading the top-level `denied` list
        # would treat a truncated sample as the complete set — the field was
        # declared in the SDK and never emitted here, so it always read false.
        "denied-truncated": axes["workspace"]["denied-truncated"],
    }


# ── The reverse view: who can reach THIS resource (#1456) ──────────────

#: Platform paths that grant on a resource regardless of any custom role. They
#: are reported alongside the roles because a list of roles alone reads as the
#: complete answer when it is not — and "who can touch this" answered
#: incompletely is worse than not answered.
PATH_OWNER = "owner"
PATH_PLATFORM_ADMIN = "platform-admin"
PATH_PLATFORM_AUDIT = "platform-audit"
PATH_EVERYONE_FLOOR = "everyone-floor"
PATH_CATALOG_CLAMP = "catalog-clamped"


async def resolve_resource_access(
    db: AsyncSession,
    obj: Any,
    *,
    axis: str,
    kind: str,
    include_holders: bool = True,
    viewer_roles: list[str] | None = None,
) -> dict[str, Any]:
    """Which roles reach one resource, at what capability, and who holds them.

    The inverse of `preview_role_reach`, and much the cheaper direction: roles
    are few, so this evaluates every custom role against ONE resource rather
    than one role against the estate. No pagination is needed and none is
    offered.

    Built on the same `role_match_verdict` as enforcement — a view of who can
    reach something must not be able to disagree with what actually happens.
    """
    assert_viewer_sees_everything(viewer_roles)
    roles = list((await db.execute(select(Role))).scalars().all())

    holders: dict[str, list[str]] = {}
    if include_holders:
        rows = (await db.execute(select(RoleAssignment))).all()
        for (ra,) in rows:
            holders.setdefault(ra.role_name, []).append(ra.email)

    granted: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    for role in roles:
        # `_entry` recomputes the verdict and carries the reason, which is what
        # line ~392 reads; only the early-continue needs the verdict here.
        verdict, _ = role_match_verdict(role, obj.name, obj.labels or {})
        if verdict == MATCH_NONE:
            continue
        entry = _entry(role, axis, kind, obj)
        row = {
            "role": role.name,
            "verdict": entry["verdict"],
            "reason": entry["reason"],
            "capabilities": entry["capabilities"],
            "notes": entry["notes"],
            "held-by": sorted(holders.get(role.name, [])),
        }
        (granted if verdict == MATCH_ALLOWED else denied).append(row)

    # Paths that do not come from a custom role at all.
    paths: list[str] = [PATH_PLATFORM_ADMIN, PATH_PLATFORM_AUDIT]
    if getattr(obj, "owner_email", ""):
        paths.append(PATH_OWNER)
    if axis != "catalog" and (obj.labels or {}).get("access") == "everyone":
        paths.append(PATH_EVERYONE_FLOOR)
    if axis == "workspace" and getattr(obj, "catalog_item_id", None) is not None:
        paths.append(PATH_CATALOG_CLAMP)

    return {
        "resource": {
            "id": f"{_ID_PREFIX.get(kind, '')}{obj.id}",
            "kind": kind,
            "name": obj.name,
            "labels": obj.labels or {},
            "owner-email": getattr(obj, "owner_email", "") or "",
        },
        "axis": axis,
        "roles": sorted(granted, key=lambda r: r["role"]),
        "denied-roles": sorted(denied, key=lambda r: r["role"]),
        "role-count": len(granted),
        "platform-paths": paths,
    }
