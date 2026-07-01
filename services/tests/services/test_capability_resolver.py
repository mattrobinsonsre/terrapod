"""Faithfulness proof for capability resolution (#585, enforcement slice).

The capability resolver must, for every preset role shape, resolve to EXACTLY
the capability set ``expand_preset`` gives for that role's level. This is the
"before == after" guarantee: switching gates from level thresholds to capability
membership changes nothing for existing preset roles.

The legacy scalar level-resolvers were deleted with #585, so the expected set is
computed directly from ``expand_preset`` (the single source of truth for the
level → capability mapping) rather than compared against a resolver oracle.

No DB — roles are passed via ``preloaded_roles`` (the resolvers skip the query).
"""

from types import SimpleNamespace

import pytest

from terrapod.auth import capabilities as cap
from terrapod.services import capability_resolver as cr

pytestmark = pytest.mark.asyncio


def _role(name, *, ws="read", pool="read", reg="read", cat="none", caps=None, **kw):
    # A role's grant is ONLY its stored ``capabilities`` (#585); build them from
    # the requested levels via expand_preset unless an explicit granular set is
    # given, so a "level" role still resolves to that level's capabilities.
    if caps is None:
        caps = cap.expand_preset(
            workspace_permission=ws,
            pool_permission=pool,
            registry_permission=reg,
            catalog_permission=cat,
        )
    return SimpleNamespace(
        name=name,
        capabilities=caps,
        allow_labels=kw.get("allow_labels", {}),
        allow_names=kw.get("allow_names", []),
        deny_labels=kw.get("deny_labels", {}),
        deny_names=kw.get("deny_names", []),
    )


def _caps_from_level(axis, level):
    """expand_preset restricted to one axis == the axis' caps for that level."""
    kwargs = {
        "workspace_permission": None,
        "pool_permission": None,
        "registry_permission": None,
        "catalog_permission": None,
    }
    kwargs[f"{axis}_permission"] = level
    return frozenset(cap.expand_preset(**kwargs))


# ── Per-axis, per-level equivalence via a single label-matched custom role ─────


@pytest.mark.parametrize("level", ["read", "plan", "write", "admin"])
async def test_workspace_level_equivalence(level):
    role = _role("r", ws=level, allow_labels={"team": ["x"]})
    caps = await cr.resolve_capabilities(
        None, "u@x", ["r"], "w1", {"team": "x"}, None, axis="workspace", preloaded_roles=[role]
    )
    assert caps == _caps_from_level("workspace", level)


@pytest.mark.parametrize("level", ["read", "write", "admin"])
async def test_pool_level_equivalence(level):
    role = _role("r", pool=level, allow_labels={"team": ["x"]})
    caps = await cr.resolve_capabilities(
        None, "u@x", ["r"], "p1", {"team": "x"}, None, axis="pool", preloaded_roles=[role]
    )
    assert caps == _caps_from_level("pool", level)


@pytest.mark.parametrize("level", ["read", "write", "admin"])
async def test_registry_level_equivalence(level):
    role = _role("r", reg=level, allow_labels={"team": ["x"]})
    caps = await cr.resolve_capabilities(
        None, "u@x", ["r"], "m1", {"team": "x"}, "", axis="registry", preloaded_roles=[role]
    )
    assert caps == _caps_from_level("registry", level)


@pytest.mark.parametrize("level", ["none", "read", "use", "admin"])
async def test_catalog_level_equivalence(level):
    role = _role("r", cat=level, allow_labels={"team": ["x"]})
    caps = await cr.resolve_capabilities(
        None, "u@x", ["r"], "c1", {"team": "x"}, None, axis="catalog", preloaded_roles=[role]
    )
    assert caps == _caps_from_level("catalog", level)


# ── Special principals ────────────────────────────────────────────────────────


async def test_admin_gets_full_axis_caps():
    for axis in cr.AXES:
        caps = await cr.resolve_capabilities(
            None, "a@x", ["admin"], "r1", {}, None, axis=axis, preloaded_roles=[]
        )
        assert caps == cap.axis_all_caps(axis)


async def test_audit_gets_read_floor():
    for axis in cr.AXES:
        caps = await cr.resolve_capabilities(
            None, "a@x", ["audit"], "r1", {}, None, axis=axis, preloaded_roles=[]
        )
        assert caps == cap.axis_read_caps(axis)  # catalog read floor is {catalog:read} for audit


async def test_owner_gets_admin_equiv_and_catalog_clamp():
    # Non-catalog workspace owner -> full workspace caps (admin-equivalent).
    caps = await cr.resolve_capabilities(
        None, "o@x", [], "w1", {}, "o@x", axis="workspace", preloaded_roles=[]
    )
    assert caps == cap.axis_all_caps("workspace")
    # Catalog-managed workspace owner -> clamped to read.
    clamped = await cr.resolve_capabilities(
        None,
        "o@x",
        [],
        "w1",
        {},
        "o@x",
        axis="workspace",
        preloaded_roles=[],
        is_catalog_managed=True,
    )
    assert clamped == cap.axis_read_caps("workspace")


async def test_everyone_floor_and_no_catalog_floor():
    labels = {"access": "everyone"}
    ws = await cr.resolve_capabilities(
        None, "u@x", [], "w1", labels, None, axis="workspace", preloaded_roles=[]
    )
    assert ws == cap.axis_read_caps("workspace")
    # Catalog has no everyone-floor.
    catalog = await cr.resolve_capabilities(
        None, "u@x", [], "c1", labels, None, axis="catalog", preloaded_roles=[]
    )
    assert catalog == frozenset()


async def test_registry_runner_floor():
    caps = await cr.resolve_capabilities(
        None,
        "u@x",
        [],
        "m1",
        {},
        "",
        axis="registry",
        preloaded_roles=[],
        auth_method="runner_token",
    )
    assert caps == frozenset({cap.REGISTRY_READ})


async def test_deny_rule_blocks():
    role = _role("r", ws="admin", allow_labels={"team": ["x"]}, deny_names=["w1"])
    caps = await cr.resolve_capabilities(
        None, "u@x", ["r"], "w1", {"team": "x"}, None, axis="workspace", preloaded_roles=[role]
    )
    assert caps == frozenset()


async def test_multiple_roles_union_equals_max_level():
    r_plan = _role("plan", ws="plan", allow_labels={"team": ["x"]})
    r_write = _role("write", ws="write", allow_labels={"team": ["x"]})
    caps = await cr.resolve_capabilities(
        None,
        "u@x",
        ["plan", "write"],
        "w1",
        {"team": "x"},
        None,
        axis="workspace",
        preloaded_roles=[r_plan, r_write],
    )
    assert caps == _caps_from_level("workspace", "write")


async def test_granular_stored_capabilities_take_precedence():
    # A role whose stored capabilities are a non-preset subset resolves to that
    # subset (not its level expansion) — the granularity the feature enables.
    granular = sorted({cap.RUN_READ, cap.VAR_READ, cap.VAR_WRITE})
    role = _role("r", ws="admin", caps=granular, allow_labels={"team": ["x"]})
    caps = await cr.resolve_capabilities(
        None, "u@x", ["r"], "w1", {"team": "x"}, None, axis="workspace", preloaded_roles=[role]
    )
    assert caps == frozenset(granular)


# ── Token attenuation (kind-aware _for) ───────────────────────────────────────


def _user(kind, roles, pinned, email="u@x"):
    return SimpleNamespace(
        kind=kind, email=email, roles=roles, pinned_roles=pinned, auth_method="api_token"
    )


async def test_service_bound_is_intersection():
    r_write = _role("write", ws="write", allow_labels={"team": ["x"]})
    r_plan = _role("plan", ws="plan", allow_labels={"team": ["x"]})
    user = _user("service_bound", ["write"], ["plan"])
    caps = await cr.resolve_capabilities_for(
        None,
        user,
        "w1",
        {"team": "x"},
        None,
        axis="workspace",
        preloaded_roles=[r_write],
        token_preloaded_roles=[r_plan],
    )
    # min(write, plan) == plan
    assert caps == _caps_from_level("workspace", "plan")


async def test_service_detached_is_token_only():
    r_write = _role("write", ws="write", allow_labels={"team": ["x"]})
    r_plan = _role("plan", ws="plan", allow_labels={"team": ["x"]})
    user = _user("service_detached", ["write"], ["plan"])
    caps = await cr.resolve_capabilities_for(
        None,
        user,
        "w1",
        {"team": "x"},
        None,
        axis="workspace",
        preloaded_roles=[r_write],
        token_preloaded_roles=[r_plan],
    )
    assert caps == _caps_from_level("workspace", "plan")
