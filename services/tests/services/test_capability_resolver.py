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

import pathlib
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
        # Explicit: `role_match_verdict` reads the column directly so a double
        # that omits it fails loudly rather than matching everything.
        allow_all=kw.get("allow_all", False),
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
    # Read floor PLUS `inventory:write` (#2019). The clamp's reason is unchanged
    # and still holds for everything else -- a catalog workspace's configuration
    # and variables are catalog-managed, so a write over either is an affordance
    # for an edit the next reconfigure discards. The inventory is the one part a
    # person legitimately curates on such a workspace, and withholding it made
    # the per-row inventory UI unreachable on exactly the workspaces the feature
    # creates, for the person who created them.
    assert clamped == cap.axis_read_caps("workspace") | {cap.INVENTORY_WRITE}


class TestTheCatalogClampIsSurgical:
    """#2019 exempts `inventory:write` from the clamp, and nothing else.

    Every test here needs the CONTRAST: a test asserting only that
    `inventory:write` survives cannot distinguish a surgical exemption from a
    clamp that stopped clamping, which is the failure that matters.
    """

    async def _clamped(self):
        return await cr.resolve_capabilities(
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

    @pytest.mark.asyncio
    async def test_the_provisioner_may_write_the_inventory(self):
        assert cap.INVENTORY_WRITE in await self._clamped()

    @pytest.mark.asyncio
    async def test_and_still_may_not_write_the_variables_or_the_config(self):
        """The two the clamp exists for: `_materialise` replaces the variable set
        outright and `config_versions` refuses an operator-supplied CV, so a
        write over either edits something the next reconfigure discards."""
        caps = await self._clamped()
        assert cap.VAR_WRITE not in caps
        assert cap.CONFIG_UPLOAD not in caps
        assert cap.WORKSPACE_SETTINGS not in caps

    @pytest.mark.asyncio
    async def test_exactly_one_capability_is_added(self):
        """Derived, so a second exemption has to justify itself in the set
        beside the clamp rather than widen a comparison mid-resolver."""
        caps = await self._clamped()
        assert caps - cap.axis_read_caps("workspace") == {cap.INVENTORY_WRITE}

    def test_the_rule_has_ONE_producer(self):
        """The clamp was written twice and diverged, which is why it is a shared
        function now (#2019).

        `capability_resolver` enforces and `role_reach_service` answers "what can
        this role actually do here" for the UI, and each had its own
        `caps & axis_read_caps(axis)`. Exempting a capability in one left the
        other reporting it clamped away: the API granting a write the view said
        was withheld, which is worse than either answer alone.

        Derived from the source rather than from a list of the two modules, so a
        THIRD copy fails here rather than being found by its divergence.
        """
        import re

        root = pathlib.Path(cr.__file__).resolve().parent.parent
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "capabilities.py":
                continue  # the producer itself
            src = path.read_text(encoding="utf-8")
            for line in src.splitlines():
                if re.search(r"&\s*cap(?:abilities)?\.axis_read_caps\(", line):
                    offenders.append(f"{path.name}: {line.strip()}")
        assert not offenders, (
            "the catalog clamp is being computed outside "
            "`capabilities.clamp_for_catalog_workspace`:\n  " + "\n  ".join(offenders)
        )

    @pytest.mark.asyncio
    async def test_the_two_surfaces_agree(self):
        """Not a restatement of the above: that one asserts nobody re-derives the
        rule, this one asserts the shared answer is the one both give."""
        enforced = await self._clamped()
        reached = cap.clamp_for_catalog_workspace(cap.axis_all_caps("workspace"), "workspace")
        assert enforced == reached

    @pytest.mark.asyncio
    async def test_a_non_catalog_workspace_is_unaffected(self):
        """The owner of an ordinary workspace still gets the whole axis -- the
        exemption must not be a widening of what the clamp applies to."""
        caps = await cr.resolve_capabilities(
            None, "o@x", [], "w1", {}, "o@x", axis="workspace", preloaded_roles=[]
        )
        assert caps == cap.axis_all_caps("workspace")


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
