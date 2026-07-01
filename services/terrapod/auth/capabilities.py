"""Capability catalog + preset expansion for capability-based RBAC (#585).

The capability is the unit of permission (resource × verb), NOT an API endpoint
(endpoints over-split multi-call capabilities like ``state:write`` and
under-split payload-polymorphic ones like apply-vs-apply-destroy). Roles carry an
explicit set of capabilities; the legacy hierarchical levels
(``workspace_permission`` etc.) become **presets** over this set.

This module is the single source of truth for the legacy-level → capability
mapping. It is used by BOTH:
  * the Alembic migration that expands existing role rows, and
  * the roles router, when a role is written with the legacy level fields
    (presets are input sugar that expand on write; capabilities are the stored,
    enforced truth).
so the two can never drift.

FAITHFULNESS CONTRACT (the reason every token below is pinned to a tier):
Each capability maps to exactly one real route gate, and every preset level is
the *union of the capabilities for every gate at or below that level today*. So
``expand_preset(workspace_permission="plan", ...)`` grants precisely what a
``plan`` role can do now — no more, no less. The mapping is grounded in the
actual ``has_permission`` / ``_require_ws_permission`` / ``has_pool_permission``
/ ``has_registry_permission`` / ``has_catalog_permission`` gate sites; see
``docs/rbac-capabilities.md`` for the full gate→capability table this is
derived from.

SCOPE (#585): capability enforcement covers the FOUR label-scoped axes that
custom roles actually carry — workspace, pool, registry, catalog. The
platform-admin gates (``require_admin`` / ``require_admin_or_audit``) stay
role-name based; decomposing the monolithic platform admin into the scoped
``platform:*`` capabilities below is a deliberate follow-up (#642), so those
tokens are listed for honesty of the built-in ``admin`` capability set but are
NOT independently grantable or enforced yet.
"""

from __future__ import annotations

# ── Catalog ───────────────────────────────────────────────────────────────────
# A capability is "<resource>:<verb>". Keep this list as the authoritative
# enumeration; new endpoints map onto an existing capability or add one here
# deliberately. Net-new capabilities that no legacy preset grants until an admin
# opts in (state:read-outputs / state:read-sensitive tier; the scoped platform:*
# split #642) are introduced by their own follow-ups.

# ── Workspace / runs — read tier ────────────────────────────────────────────
# Per-resource read caps (each paired with a write/manage cap below, so an
# operator can grant read-only on one resource type independently). Today's
# "read" level grants ALL of them.
WORKSPACE_READ = "workspace:read"  # show/list workspace, vcs-refs, tag-bindings
RUN_READ = "run:read"  # runs, plans, applies, run-events, AI summary+chat, SSE
STATE_READ_METADATA = "state:read-metadata"  # list/show state versions, current
VAR_READ = "var:read"  # list variables (values masked when sensitive)
CONFIG_READ = "config:read"  # list/download configuration versions, diff, ticket
RUN_TASK_READ = "run-task:read"  # list/show run tasks + task stages
NOTIFICATION_READ = "notification:read"  # list/show notification configs
RUN_TRIGGER_READ = "run-trigger:read"  # list/show run triggers

# ── Workspace / runs — plan tier ────────────────────────────────────────────
RUN_PLAN = "run:plan"  # create a plan-only run
RUN_CANCEL = "run:cancel"  # discard / cancel / retry a run
WORKSPACE_LOCK = "workspace:lock"  # lock / unlock own lock (state lock)
STATE_READ = "state:read"  # download RAW state JSON (contains secrets)
DRIFT_DISMISS = "drift:dismiss"  # dismiss a workspace drift flag

# ── Workspace / runs — write tier ───────────────────────────────────────────
RUN_APPLY = "run:apply"  # create an apply-capable run + confirm apply
RUN_APPLY_DESTROY = "run:apply-destroy"  # create/confirm a destroy run (is_destroy)
VAR_WRITE = "var:write"  # create / update / delete variables
STATE_WRITE = "state:write"  # create state version, manual upload, rollback
CONFIG_UPLOAD = "config:upload"  # create a configuration version

# ── Workspace / runs — admin tier ───────────────────────────────────────────
WORKSPACE_SETTINGS = "workspace:settings"  # PATCH workspace (settings/VCS/labels/pool)
WORKSPACE_FORCE_UNLOCK = "workspace:force-unlock"  # unlock a lock held by someone else
WORKSPACE_DELETE = "workspace:delete"  # delete the workspace
STATE_DELETE = "state:delete"  # delete a non-current state version
NOTIFICATION_MANAGE = "notification:manage"  # create/update/delete/verify notif configs
RUN_TASK_MANAGE = "run-task:manage"  # create/update/delete run tasks + stage override
RUN_TRIGGER_MANAGE = "run-trigger:manage"  # create/delete run triggers

# ── Pool ────────────────────────────────────────────────────────────────────
POOL_READ = "pool:read"  # show pool, list listeners, pool event stream
POOL_ASSIGN = "pool:assign"  # assign the pool to a workspace (write tier)
POOL_MANAGE = "pool:manage"  # update/delete pool, manage join tokens, delete listeners

# ── Registry ────────────────────────────────────────────────────────────────
REGISTRY_READ = "registry:read"  # list/show/download modules + providers
REGISTRY_WRITE = "registry:write"  # create versions, upload tarballs/binaries
REGISTRY_ADMIN = "registry:admin"  # delete modules/providers/versions, VCS, links

# ── Catalog ─────────────────────────────────────────────────────────────────
CATALOG_READ = "catalog:read"  # browse catalog items + own instances
CATALOG_USE = "catalog:use"  # provision / reconfigure / destroy an instance
CATALOG_ADMIN = "catalog:admin"  # orphan-delete an instance (item authoring is platform)

# ── Platform (informational only until #642 — NOT yet enforced/grantable) ────
# The monolithic platform admin's constituent powers, grouped by the survey of
# require_admin gates. Listed so capabilities_for_builtin("admin") is honest and
# the scoped split (#642) has its target vocabulary; platform gates stay
# role-name based in this feature.
PLATFORM_ROLE_ADMIN = "platform:role-admin"  # roles + role assignments
PLATFORM_VCS_ADMIN = "platform:vcs-admin"  # VCS connections
PLATFORM_POOL_ADMIN = "platform:pool-admin"  # create agent pools, orphan-listener cleanup
PLATFORM_REGISTRY_ADMIN = (
    "platform:registry-admin"  # binary/provider cache, GPG keys, owner reassign
)
PLATFORM_VARSET_ADMIN = "platform:varset-admin"  # variable sets (org-scoped)
PLATFORM_USER_ADMIN = "platform:user-admin"  # user CRUD + password reset
PLATFORM_AUDIT_ADMIN = "platform:audit-admin"  # read the audit log
PLATFORM_POLICY_ADMIN = "platform:policy-admin"  # OPA policy sets
PLATFORM_AUTODISCOVERY_ADMIN = "platform:autodiscovery-admin"  # autodiscovery rules
PLATFORM_CATALOG_ADMIN = "platform:catalog-admin"  # catalog item + provider-template authoring
PLATFORM_BULK_ADMIN = "platform:bulk-admin"  # bulk workspace operations
PLATFORM_SETTINGS_ADMIN = "platform:settings-admin"  # platform settings (e.g. state-encryption)

PLATFORM_CAPABILITIES: frozenset[str] = frozenset(
    {
        PLATFORM_ROLE_ADMIN,
        PLATFORM_VCS_ADMIN,
        PLATFORM_POOL_ADMIN,
        PLATFORM_REGISTRY_ADMIN,
        PLATFORM_VARSET_ADMIN,
        PLATFORM_USER_ADMIN,
        PLATFORM_AUDIT_ADMIN,
        PLATFORM_POLICY_ADMIN,
        PLATFORM_AUTODISCOVERY_ADMIN,
        PLATFORM_CATALOG_ADMIN,
        PLATFORM_BULK_ADMIN,
        PLATFORM_SETTINGS_ADMIN,
    }
)

# ── Per-axis level → capability maps (cumulative: each level ⊇ lower) ──────────
# Faithful to today's enforced gates (verified against the routers' has_permission
# / _require_ws_permission sites; see docs/rbac-capabilities.md for the full
# gate→capability table and the #585 before/after proof).

_WORKSPACE_LEVELS: dict[str, frozenset[str]] = {
    "read": frozenset(
        {
            WORKSPACE_READ,
            RUN_READ,
            STATE_READ_METADATA,
            VAR_READ,
            CONFIG_READ,
            RUN_TASK_READ,
            NOTIFICATION_READ,
            RUN_TRIGGER_READ,
        }
    ),
}
_WORKSPACE_LEVELS["plan"] = _WORKSPACE_LEVELS["read"] | {
    RUN_PLAN,
    RUN_CANCEL,
    WORKSPACE_LOCK,
    STATE_READ,
    DRIFT_DISMISS,
}
_WORKSPACE_LEVELS["write"] = _WORKSPACE_LEVELS["plan"] | {
    RUN_APPLY,
    RUN_APPLY_DESTROY,
    VAR_WRITE,
    STATE_WRITE,
    CONFIG_UPLOAD,
}
_WORKSPACE_LEVELS["admin"] = _WORKSPACE_LEVELS["write"] | {
    WORKSPACE_SETTINGS,
    WORKSPACE_FORCE_UNLOCK,
    WORKSPACE_DELETE,
    STATE_DELETE,
    NOTIFICATION_MANAGE,
    RUN_TASK_MANAGE,
    RUN_TRIGGER_MANAGE,
}

_POOL_LEVELS: dict[str, frozenset[str]] = {
    "read": frozenset({POOL_READ}),
    "write": frozenset({POOL_READ, POOL_ASSIGN}),
    "admin": frozenset({POOL_READ, POOL_ASSIGN, POOL_MANAGE}),
}

_REGISTRY_LEVELS: dict[str, frozenset[str]] = {
    "read": frozenset({REGISTRY_READ}),
    "write": frozenset({REGISTRY_READ, REGISTRY_WRITE}),
    "admin": frozenset({REGISTRY_READ, REGISTRY_WRITE, REGISTRY_ADMIN}),
}

_CATALOG_LEVELS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "read": frozenset({CATALOG_READ}),
    "use": frozenset({CATALOG_READ, CATALOG_USE}),
    "admin": frozenset({CATALOG_READ, CATALOG_USE, CATALOG_ADMIN}),
}

# Full enumeration of every grantable (label-scoped) capability — the union of
# all four axes' top levels. Used to validate capability lists on role write
# (reject unknown / not-yet-grantable tokens like platform:* and the net-new
# sensitive-state tier) and by tests asserting the catalog is complete.
GRANTABLE_CAPABILITIES: frozenset[str] = frozenset(
    _WORKSPACE_LEVELS["admin"]
    | _POOL_LEVELS["admin"]
    | _REGISTRY_LEVELS["admin"]
    | _CATALOG_LEVELS["admin"]
)


# ── Forward-compat: capability aliases ────────────────────────────────────────
# Roles store a frozen snapshot of capability strings in JSONB. If a future
# release renames or splits a capability, stored roles still carry the OLD token.
# To avoid silently dropping it (retroactive tightening — forbidden) or trapping
# the role uneditable on the next write-path validation (the #316 reserved-label
# failure mode), every stored set is run through ``normalize_capabilities`` at
# load AND before write-validation: legacy tokens are upgraded to their current
# equivalent(s) here. Empty until the first rename; e.g. a future split would be
#   "state:read": frozenset({"state:read-outputs", "state:read-sensitive"}).
CAPABILITY_ALIASES: dict[str, frozenset[str]] = {}


def normalize_capabilities(caps: list[str] | set[str] | frozenset[str]) -> list[str]:
    """Upgrade any legacy/aliased capability tokens to their current form.

    Applied at load and before write-validation so a role written under an older
    catalog keeps working after a capability rename/split. Unknown tokens with no
    alias are preserved (never silently dropped — that would tighten the role);
    write-path validation decides whether a still-unknown token is a hard error.
    Returns a sorted, de-duplicated list.
    """
    out: set[str] = set()
    for c in caps:
        if c in CAPABILITY_ALIASES:
            out |= CAPABILITY_ALIASES[c]
        else:
            out.add(c)
    return sorted(out)


_LEVEL_TO_AXES: dict[str, dict[str, str]] = {
    "read": {"w": "read", "p": "read", "r": "read", "c": "read"},
    "plan": {"w": "plan", "p": "read", "r": "read", "c": "read"},
    "write": {"w": "write", "p": "write", "r": "write", "c": "use"},
    "admin": {"w": "admin", "p": "admin", "r": "admin", "c": "admin"},
    "use": {"c": "use"},
    "none": {},
}


def caps_for_level(level: str | None) -> frozenset[str]:
    """The capability set a legacy permission level maps to, unioned across all
    four axes. A given axis's gate only checks its own axis's capability, so this
    union is a faithful stand-in for any single-axis resolver at ``level`` (used
    by tests that mock a resolver, and by UX that renders a preset's caps).
    ``None`` / unknown → empty (no access)."""
    if not level:
        return frozenset()
    m = _LEVEL_TO_AXES.get(level, {})
    return frozenset(
        expand_preset(
            workspace_permission=m.get("w"),
            pool_permission=m.get("p"),
            registry_permission=m.get("r"),
            catalog_permission=m.get("c"),
        )
    )


def has_capability(caps: frozenset[str] | set[str], required: str) -> bool:
    """Canonical capability membership check (the enforcement primitive).

    ENFORCEMENT CONTRACT: a route gate maps to exactly ONE capability — the verb
    it performs — and checks membership of THAT capability, e.g.
    ``has_capability(caps, RUN_PLAN)`` for queue-plan. It does NOT check "holds
    the whole write preset", which would re-introduce the cumulative coupling
    this feature removes. The faithfulness test asserts per-gate, not per-level.
    """
    return required in caps


# Public per-axis level maps, keyed by short axis name — for the capability
# resolver (union / read-floor / full-set per axis). Values are the SAME frozen
# sets as the private maps above.
AXIS_LEVEL_MAPS: dict[str, dict[str, frozenset[str]]] = {
    "workspace": _WORKSPACE_LEVELS,
    "pool": _POOL_LEVELS,
    "registry": _REGISTRY_LEVELS,
    "catalog": _CATALOG_LEVELS,
}


def axis_read_caps(axis: str) -> frozenset[str]:
    """The read-floor capability set for an axis (empty for catalog — no floor)."""
    return AXIS_LEVEL_MAPS[axis].get("read", frozenset())


def axis_all_caps(axis: str) -> frozenset[str]:
    """Every capability the axis can grant (its top preset)."""
    levels = AXIS_LEVEL_MAPS[axis]
    return frozenset().union(*levels.values()) if levels else frozenset()


def expand_preset(
    *,
    workspace_permission: str | None,
    pool_permission: str | None,
    registry_permission: str | None,
    catalog_permission: str | None,
) -> list[str]:
    """Expand the legacy hierarchical levels into the explicit capability set.

    The union of the per-axis level expansions. Unknown/None values contribute
    nothing (defensive — the migration must never throw on an unexpected stored
    value). Returns a sorted list (stable on disk / in JSON).
    """
    caps: set[str] = set()
    caps |= _WORKSPACE_LEVELS.get((workspace_permission or "").strip(), frozenset())
    caps |= _POOL_LEVELS.get((pool_permission or "").strip(), frozenset())
    caps |= _REGISTRY_LEVELS.get((registry_permission or "").strip(), frozenset())
    caps |= _CATALOG_LEVELS.get((catalog_permission or "").strip(), frozenset())
    return sorted(caps)


# ── Inverse: capability set → derived preset summary ──────────────────────────
# The reverse of expand_preset, for the read side. Capabilities are the stored
# truth; the legacy level fields become a DERIVED, denormalised summary (the
# closest preset name per axis, or "custom"). Consumers that still read the level
# fields (go-terrapod, the provider's terrapod_role, the web roles page) get a
# faithful summary; granular sets that don't match a preset render "custom".

_AXIS_LEVELS: dict[str, dict[str, frozenset[str]]] = {
    "workspace_permission": _WORKSPACE_LEVELS,
    "pool_permission": _POOL_LEVELS,
    "registry_permission": _REGISTRY_LEVELS,
    "catalog_permission": _CATALOG_LEVELS,
}
# The capabilities owned by each axis (union of that axis' top preset) — used to
# slice a role's full set down to one axis before matching it to a preset.
_AXIS_CAPS: dict[str, frozenset[str]] = {
    axis: frozenset().union(*levels.values()) for axis, levels in _AXIS_LEVELS.items()
}


def summarize_capabilities(caps: list[str] | set[str] | frozenset[str]) -> dict[str, str]:
    """Derive the legacy per-axis level summary from a capability set.

    For each axis, slice the role's caps to that axis and find the preset whose
    expansion equals the slice exactly; if none matches, the axis is ``"custom"``.
    An empty slice maps to the axis' bottom level (``"none"`` for catalog, which
    is genuinely empty; ``"read"`` for the others is the floor preset — note an
    empty workspace slice means the role grants NO workspace caps, which is below
    even ``read``, so it is reported as ``custom`` to avoid implying a read grant).
    """
    cap_set = set(caps)
    summary: dict[str, str] = {}
    for axis, levels in _AXIS_LEVELS.items():
        slice_caps = frozenset(cap_set & _AXIS_CAPS[axis])
        match = next((name for name, exp in levels.items() if exp == slice_caps), None)
        if match is not None:
            summary[axis] = match
        elif not slice_caps:
            # Empty slice with no "empty preset": only catalog has one ("none").
            summary[axis] = "none" if "none" in levels else "custom"
        else:
            summary[axis] = "custom"
    return summary


# ── Built-in role capability sets (built-ins are code, not DB rows) ────────────
# Defined here so they get the same catalog. `admin` is the superuser preset =
# every grantable capability PLUS every platform:* power (admin bypasses RBAC
# entirely, so this set is the honest description of what it can do).

_AUDIT_CAPS = frozenset(
    {
        WORKSPACE_READ,
        RUN_READ,
        STATE_READ_METADATA,
        VAR_READ,
        CONFIG_READ,
        RUN_TASK_READ,
        NOTIFICATION_READ,
        RUN_TRIGGER_READ,
        POOL_READ,
        REGISTRY_READ,
        CATALOG_READ,
        PLATFORM_AUDIT_ADMIN,
    }
)

_BUILTIN_CAPS: dict[str, list[str]] = {
    "admin": sorted(GRANTABLE_CAPABILITIES | PLATFORM_CAPABILITIES),
    "audit": sorted(_AUDIT_CAPS),
    "everyone": sorted(_WORKSPACE_LEVELS["read"]),  # read floor under access:everyone
}


def capabilities_for_builtin(name: str) -> list[str]:
    """Capability set for a built-in role (admin / audit / everyone)."""
    return list(_BUILTIN_CAPS.get(name, []))
