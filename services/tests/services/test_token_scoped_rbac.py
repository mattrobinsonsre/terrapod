"""Token-scoped RBAC resolution (#495, capability form after #585).

Phase 2 of scoped service tokens introduces three token kinds and the
kind-aware ``resolve_*_capabilities_for`` wrappers:

* ``interactive``      -> resolve the principal's live roles (status quo).
* ``service_bound``    -> ``user_caps ∩ token_caps`` per resource, so a bound
                          token can only ever be a subset of its owner's live
                          access.
* ``service_detached`` -> ``token_caps`` alone (pinned roles only, no owner
                          identity, no everyone-floor).

#585 replaced the scalar level resolvers with capability-set resolvers, so these
tests assert on the resolved capability SETS (built from ``expand_preset``, the
level → capability mapping) rather than a single level string. The intent is
preserved: a token can only attenuate, never escalate, and the everyone-floor is
suppressed on the token side.
"""

from unittest.mock import AsyncMock, MagicMock

from terrapod.api.dependencies import AuthenticatedUser, effective_platform_roles
from terrapod.auth import capabilities as cap
from terrapod.services import (
    pool_rbac_service,
    registry_rbac_service,
    workspace_rbac_service,
)


def _ws_caps(level):
    return frozenset(
        cap.expand_preset(
            workspace_permission=level,
            pool_permission=None,
            registry_permission=None,
            catalog_permission=None,
        )
    )


def _pool_caps(level):
    return frozenset(
        cap.expand_preset(
            workspace_permission=None,
            pool_permission=level,
            registry_permission=None,
            catalog_permission=None,
        )
    )


def _reg_caps(level):
    return frozenset(
        cap.expand_preset(
            workspace_permission=None,
            pool_permission=None,
            registry_permission=level,
            catalog_permission=None,
        )
    )


def _user(*, email="dev@example.com", roles=None, kind="interactive", pinned=None):
    return AuthenticatedUser(
        email=email,
        display_name=None,
        roles=roles or [],
        provider_name="api_token",
        auth_method="api_token",
        kind=kind,
        pinned_roles=pinned,
    )


def _workspace(*, name="ws-1", labels=None, owner_email=None, catalog_item_id=None):
    ws = MagicMock()
    ws.name = name
    ws.labels = labels or {}
    ws.owner_email = owner_email
    # Explicit None so MagicMock doesn't auto-return a truthy attribute and trip
    # the catalog-managed RBAC clamp (#535).
    ws.catalog_item_id = catalog_item_id
    return ws


def _role(
    *,
    name,
    workspace_permission="read",
    pool_permission="read",
    registry_permission="read",
    allow_names=None,
):
    # A role's grant is ONLY its stored capabilities (#585); expand the requested
    # levels into a capability list, exactly as the roles router does on write.
    role = MagicMock()
    role.name = name
    role.capabilities = cap.expand_preset(
        workspace_permission=workspace_permission,
        pool_permission=pool_permission,
        registry_permission=registry_permission,
        catalog_permission=None,
    )
    role.allow_labels = {}
    role.allow_names = allow_names or []
    role.deny_labels = {}
    role.deny_names = []
    return role


def _db_with_roles(roles):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = roles
    db.execute.return_value = result
    return db


# --------------------------------------------------------------------------
# effective_platform_roles — kind attenuation of the admin/audit name-set
# --------------------------------------------------------------------------


class TestEffectivePlatformRoles:
    def test_interactive_passes_live_roles_through(self):
        u = _user(roles=["admin", "audit"], kind="interactive")
        assert effective_platform_roles(u) == {"admin", "audit"}

    def test_service_bound_intersects_live_and_pinned(self):
        # Owner is admin, token pinned to a non-admin role -> no admin.
        u = _user(roles=["admin"], kind="service_bound", pinned=["deployer"])
        assert effective_platform_roles(u) == set()

    def test_service_bound_keeps_role_in_both(self):
        u = _user(roles=["admin", "deployer"], kind="service_bound", pinned=["admin"])
        assert effective_platform_roles(u) == {"admin"}

    def test_service_detached_uses_pinned_only(self):
        # Detached carries no live-role identity; the pinned set is absolute.
        u = _user(roles=[], kind="service_detached", pinned=["admin"])
        assert effective_platform_roles(u) == {"admin"}

    def test_service_bound_cannot_synthesize_admin_from_pin_alone(self):
        # A non-admin owner pinning "admin" must NOT become admin.
        u = _user(roles=["deployer"], kind="service_bound", pinned=["admin"])
        assert effective_platform_roles(u) == set()


# --------------------------------------------------------------------------
# resolve_workspace_capabilities_for — kind-aware resolution
# --------------------------------------------------------------------------


class TestWorkspaceForResolution:
    async def test_interactive_uses_live_roles(self):
        ws = _workspace(name="ws-1")
        role = _role(name="deployer", workspace_permission="write", allow_names=["ws-1"])
        db = _db_with_roles([role])
        u = _user(roles=["deployer"], kind="interactive")
        caps = await workspace_rbac_service.resolve_workspace_capabilities_for(db, u, ws)
        assert caps == _ws_caps("write")

    async def test_service_bound_is_intersection(self):
        # Owner has write via "deployer"; token pinned to "viewer" (read).
        # write ∩ read -> read.
        ws = _workspace(name="ws-1")
        deployer = _role(name="deployer", workspace_permission="write", allow_names=["ws-1"])
        viewer = _role(name="viewer", workspace_permission="read", allow_names=["ws-1"])
        db = _db_with_roles([deployer, viewer])
        u = _user(roles=["deployer"], kind="service_bound", pinned=["viewer"])
        # Pass both sides' preloaded role pools; the resolver filters by name
        # in-code, so owner (deployer→write) and token (viewer→read) resolve
        # to their own role rather than the union.
        caps = await workspace_rbac_service.resolve_workspace_capabilities_for(
            db, u, ws, preloaded_roles=[deployer, viewer], token_preloaded_roles=[deployer, viewer]
        )
        assert caps == _ws_caps("read")

    async def test_service_bound_capped_by_owner(self):
        # Token pinned to admin role, but owner only has read -> intersection -> read.
        ws = _workspace(name="ws-1")
        viewer = _role(name="viewer", workspace_permission="read", allow_names=["ws-1"])
        superuser = _role(name="superuser", workspace_permission="admin", allow_names=["ws-1"])
        db = _db_with_roles([viewer, superuser])
        u = _user(roles=["viewer"], kind="service_bound", pinned=["superuser"])
        caps = await workspace_rbac_service.resolve_workspace_capabilities_for(
            db,
            u,
            ws,
            preloaded_roles=[viewer, superuser],
            token_preloaded_roles=[viewer, superuser],
        )
        assert caps == _ws_caps("read")

    async def test_service_bound_owner_no_access_yields_empty(self):
        # Owner can't see the workspace at all -> bound token can't either.
        ws = _workspace(name="ws-1")
        superuser = _role(name="superuser", workspace_permission="admin", allow_names=["ws-1"])
        db = _db_with_roles([superuser])
        # Owner holds no role granting ws-1; pinned role would grant admin.
        u = _user(roles=[], kind="service_bound", pinned=["superuser"])
        caps = await workspace_rbac_service.resolve_workspace_capabilities_for(db, u, ws)
        assert caps == frozenset()

    async def test_service_detached_uses_pinned_only(self):
        ws = _workspace(name="ws-1")
        superuser = _role(name="superuser", workspace_permission="admin", allow_names=["ws-1"])
        db = _db_with_roles([superuser])
        # No live roles, no owner identity — pinned admin role stands alone.
        u = _user(email="", roles=[], kind="service_detached", pinned=["superuser"])
        caps = await workspace_rbac_service.resolve_workspace_capabilities_for(db, u, ws)
        assert caps == _ws_caps("admin")

    async def test_detached_does_not_get_everyone_floor(self):
        # access:everyone grants interactive users read; a detached token with
        # no matching pinned role gets nothing (floor suppressed).
        ws = _workspace(name="ws-1", labels={"access": "everyone"})
        db = _db_with_roles([])
        detached = _user(email="", roles=[], kind="service_detached", pinned=[])
        assert (
            await workspace_rbac_service.resolve_workspace_capabilities_for(db, detached, ws)
            == frozenset()
        )
        # ...whereas an interactive everyone-holder gets the read floor.
        interactive = _user(roles=["everyone"], kind="interactive")
        assert await workspace_rbac_service.resolve_workspace_capabilities_for(
            db, interactive, ws
        ) == _ws_caps("read")

    async def test_bound_token_does_not_get_everyone_floor_on_token_side(self):
        # Owner gets read via the everyone floor; the token side has the floor
        # suppressed, so read ∩ ∅ -> ∅: a bound token cannot ride the everyone
        # floor.
        ws = _workspace(name="ws-1", labels={"access": "everyone"})
        db = _db_with_roles([])
        u = _user(roles=["everyone"], kind="service_bound", pinned=[])
        caps = await workspace_rbac_service.resolve_workspace_capabilities_for(db, u, ws)
        assert caps == frozenset()


# --------------------------------------------------------------------------
# pool + registry _for wrappers (smoke; share the workspace machinery)
# --------------------------------------------------------------------------


class TestPoolAndRegistryForResolution:
    async def test_pool_service_bound_intersection(self):
        writer = _role(name="pool-writer", pool_permission="write", allow_names=["pool-1"])
        admin_r = _role(name="pool-admin", pool_permission="admin", allow_names=["pool-1"])
        db = _db_with_roles([writer, admin_r])
        u = _user(roles=["pool-admin"], kind="service_bound", pinned=["pool-writer"])
        caps = await pool_rbac_service.resolve_pool_capabilities_for(
            db,
            u,
            pool_name="pool-1",
            pool_labels={},
            owner_email=None,
            preloaded_roles=[writer, admin_r],
            token_preloaded_roles=[writer, admin_r],
        )
        # admin ∩ write -> write
        assert caps == _pool_caps("write")

    async def test_registry_detached_pinned_only(self):
        writer = _role(name="mod-writer", registry_permission="write", allow_names=["mod-1"])
        db = _db_with_roles([writer])
        u = _user(email="", roles=[], kind="service_detached", pinned=["mod-writer"])
        caps = await registry_rbac_service.resolve_registry_capabilities_for(
            db, u, resource_name="mod-1", resource_labels={}, owner_email=""
        )
        assert caps == _reg_caps("write")

    async def test_registry_detached_ignores_owner_identity(self):
        # Even if the detached token's (empty) email matched an owner, the
        # detached path passes empty owner identity, so ownership can't apply.
        db = _db_with_roles([])
        u = _user(email="", roles=[], kind="service_detached", pinned=[])
        caps = await registry_rbac_service.resolve_registry_capabilities_for(
            db, u, resource_name="mod-1", resource_labels={}, owner_email=""
        )
        assert caps == frozenset()
