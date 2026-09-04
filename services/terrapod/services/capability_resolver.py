"""Capability-set resolution for capability-based RBAC (#585).

Parallel to the scalar level resolvers (``workspace_rbac_service`` etc.): instead
of the single highest LEVEL a principal has on a resource, this returns the SET
of capabilities they hold on it, so gates can check one capability
(``has_capability(caps, RUN_PLAN)``) rather than a level threshold.

A role's contribution is its persisted ``capabilities`` — the single source of
truth (the migration back-fills it; create/update expand the level shorthand into
it; the level columns no longer exist). Sliced to the axis being
resolved. Owner / platform-admin / audit / everyone-floor / runner-floor /
catalog-managed-clamp / token-attenuation all mirror the scalar resolvers
exactly; the equivalence test (``test_capability_resolver``) asserts, for every
preset role shape, that this layer agrees with ``expand_preset(scalar_level)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth import capabilities as cap
from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role
from terrapod.services.rbac_service import first_matching_label, merge_labels

if TYPE_CHECKING:
    from terrapod.api.dependencies import AuthenticatedUser

AXES = ("workspace", "pool", "registry", "catalog")


def role_effective_capabilities(role: Role) -> frozenset[str]:
    """A role's capability set — its persisted ``capabilities`` (the single source
    of truth; the migration back-fills it and create/update expand the level
    shorthand into it). Normalised (aliases upgraded)."""
    return frozenset(cap.normalize_capabilities(role.capabilities))


#: Verdicts from ``role_match_verdict``. A role either has no rule reaching the
#: resource, is excluded by a deny rule, or is allowed.
MATCH_NONE = "none"
MATCH_DENIED = "denied"
MATCH_ALLOWED = "allowed"


def role_match_verdict(
    role: Role, resource_name: str, resource_labels: dict, *, unscoped: bool = False
) -> tuple[str, str]:
    """The deny-then-allow gate, with the reason it landed where it did.

    Returns ``(verdict, reason)`` where verdict is one of MATCH_NONE /
    MATCH_DENIED / MATCH_ALLOWED and reason is a short machine-readable string
    naming the rule responsible (``deny-name``, ``allow-label:env=prod``, …) or
    ``""`` when nothing matched.

    ``_role_matches`` is a thin wrapper over this, so enforcement and the
    role-reach preview cannot disagree: there is one gate, and the preview
    reads its reasoning rather than recomputing it.

    See ``_role_matches`` for the meaning of ``unscoped``.
    """
    deny_labels: dict[str, set[str]] = {}
    merge_labels(deny_labels, role.deny_labels)
    if resource_name in set(role.deny_names):
        return MATCH_DENIED, "deny-name"
    hit = first_matching_label(resource_labels, deny_labels)
    if hit is not None:
        return MATCH_DENIED, f"deny-label:{hit[0]}={hit[1]}"

    # Estate-wide grant, checked AFTER deny so "everything except the sealed
    # ones" stays expressible. It grants the role's capabilities everywhere; it
    # does not raise them.
    # Read the column directly rather than getattr(..., False): a test double
    # that forgets the attribute should fail loudly, not silently match
    # everything — an unset MagicMock attr is truthy, which would turn every
    # role into an estate-wide grant.
    if role.allow_all:
        return MATCH_ALLOWED, "allow-all"

    allow_labels: dict[str, set[str]] = {}
    merge_labels(allow_labels, role.allow_labels)
    if resource_name in set(role.allow_names):
        return MATCH_ALLOWED, "allow-name"
    hit = first_matching_label(resource_labels, allow_labels)
    if hit is not None:
        return MATCH_ALLOWED, f"allow-label:{hit[0]}={hit[1]}"
    if unscoped and not role.allow_names and not allow_labels:
        return MATCH_ALLOWED, "unscoped"
    return MATCH_NONE, ""


def _role_matches(
    role: Role, resource_name: str, resource_labels: dict, *, unscoped: bool = False
) -> bool:
    """Deny-then-allow label/name match (the shared per-role gate).

    `unscoped` is for a resource that is the axis ITSELF rather than a thing
    within it — the GPG signing-key store is the only one today (#1300). Such a
    resource has no name, no labels and no owner, so the ordinary allow rules
    can never match it: `allow_names` has nothing to compare against and
    `matches_labels` requires the key to be present on the resource. Without
    this flag the capability is reachable only by platform admin, whatever a
    role was granted — which makes the `registry:admin` name a lie.

    A role matches an unscoped resource only when it is itself unscoped: no
    `allow_names` and no `allow_labels`. That distinction is the point. A role
    granted authority over *some* providers must not be able to register a
    signing key, because a key is a trust anchor for the whole registry — every
    provider signed by it becomes installable, including ones outside that
    role's scope. A role granted registry admin over everything already has
    that reach, so letting it manage keys widens nothing.

    Deny rules are inert here for the same reason allow rules are — there is no
    name or label to match — so an unscoped role cannot be excluded by them.
    That is why the unscoped grant is deliberately narrow: it is all-or-nothing
    by construction.
    """
    verdict, _ = role_match_verdict(role, resource_name, resource_labels, unscoped=unscoped)
    return verdict == MATCH_ALLOWED


async def resolve_capabilities(
    db: AsyncSession,
    user_email: str,
    user_roles: list[str],
    resource_name: str,
    resource_labels: dict,
    owner_email: str | None,
    *,
    axis: str,
    preloaded_roles: list[Role] | None = None,
    apply_everyone_floor: bool = True,
    auth_method: str = "",
    is_catalog_managed: bool = False,
    unscoped: bool = False,
) -> frozenset[str]:
    """The capability set a principal holds on a resource, for one axis.

    Mirrors the scalar resolver's order (platform admin > audit > runner-floor
    (registry) > owner > label RBAC > everyone-floor) but accumulates capability
    sets. ``is_catalog_managed`` (workspace axis) clamps every non-platform-admin
    grant to the read floor, exactly as the scalar catalog clamp does.
    """
    all_caps = cap.axis_all_caps(axis)
    read_caps = cap.axis_read_caps(axis)
    role_set = set(user_roles)

    # 1. Platform admin → full axis caps, bypassing the catalog clamp (admins
    #    manage catalog-managed workspaces directly), matching scalar's early
    #    return of "admin".
    if "admin" in role_set:
        return all_caps

    caps: set[str] = set()

    # 2. Platform audit → read floor.
    if "audit" in role_set:
        caps |= read_caps

    # 3. Registry runner-token floor (runners download modules/providers).
    if axis == "registry" and auth_method == "runner_token":
        caps |= read_caps

    # 4. Owner → full axis caps (the catalog clamp below reduces this to read on
    #    a catalog-managed workspace, matching scalar's owner→read branch there).
    if owner_email and owner_email == user_email:
        caps |= all_caps

    # 5. Label-based RBAC from custom roles.
    custom_role_names = role_set - BUILTIN_ROLE_NAMES
    if custom_role_names:
        if preloaded_roles is not None:
            roles = [r for r in preloaded_roles if r.name in custom_role_names]
        else:
            result = await db.execute(select(Role).where(Role.name.in_(custom_role_names)))
            roles = list(result.scalars().all())
        for role in roles:
            if _role_matches(role, resource_name, resource_labels, unscoped=unscoped):
                caps |= role_effective_capabilities(role) & all_caps

    # 6. everyone-floor: read on resources labelled access=everyone. Catalog has
    #    no floor (opt-in axis), matching the scalar catalog resolver.
    if axis != "catalog" and apply_everyone_floor and resource_labels.get("access") == "everyone":
        caps |= read_caps

    # 7. catalog-managed workspace clamp → read floor for all non-admin grants.
    if axis == "workspace" and is_catalog_managed:
        caps &= read_caps

    return frozenset(caps)


async def resolve_capabilities_for(
    db: AsyncSession,
    user: AuthenticatedUser,
    resource_name: str,
    resource_labels: dict,
    owner_email: str | None,
    *,
    axis: str,
    preloaded_roles: list[Role] | None = None,
    token_preloaded_roles: list[Role] | None = None,
    is_catalog_managed: bool = False,
    unscoped: bool = False,
) -> frozenset[str]:
    """Kind-aware capability set (#495): interactive → user roles;
    service_bound → user ∩ token; service_detached → token only (no owner
    identity, everyone/runner floors suppressed)."""
    auth_method = getattr(user, "auth_method", "") or ""
    if user.kind == "service_detached":
        return await resolve_capabilities(
            db,
            "",
            user.pinned_roles or [],
            resource_name,
            resource_labels,
            owner_email,
            axis=axis,
            preloaded_roles=token_preloaded_roles,
            apply_everyone_floor=False,
            auth_method="",
            is_catalog_managed=is_catalog_managed,
            unscoped=unscoped,
        )

    user_caps = await resolve_capabilities(
        db,
        user.email,
        user.roles,
        resource_name,
        resource_labels,
        owner_email,
        axis=axis,
        preloaded_roles=preloaded_roles,
        auth_method=auth_method,
        is_catalog_managed=is_catalog_managed,
        unscoped=unscoped,
    )
    if user.kind == "service_bound":
        token_caps = await resolve_capabilities(
            db,
            "",
            user.pinned_roles or [],
            resource_name,
            resource_labels,
            owner_email,
            axis=axis,
            preloaded_roles=token_preloaded_roles,
            apply_everyone_floor=False,
            auth_method="",
            is_catalog_managed=is_catalog_managed,
            unscoped=unscoped,
        )
        return user_caps & token_caps
    return user_caps
