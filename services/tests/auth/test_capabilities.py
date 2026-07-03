"""Capability catalog + preset-expansion tests (#585, data layer).

The expansion is the migration contract: each level expands to exactly what it
grants, and is a faithful superset of the level below. (Enforcement still uses
the levels in this phase; the route-gate-anchored equality test arrives with the
PR that switches resolution to capabilities.)
"""

from terrapod.auth import capabilities as cap


def _ws(level):
    return set(
        cap.expand_preset(
            workspace_permission=level,
            pool_permission=None,
            registry_permission=None,
            catalog_permission=None,
        )
    )


def test_workspace_levels_are_cumulative():
    read, plan, write, admin = _ws("read"), _ws("plan"), _ws("write"), _ws("admin")
    assert read < plan < write < admin, (
        "each workspace level must be a strict superset of the one below"
    )


def test_workspace_read_tier_membership():
    read = _ws("read")
    # Per-resource read caps (each paired with a write/manage cap), but NOT raw
    # state read (that is plan-tier) or any verb above read.
    assert {
        cap.WORKSPACE_READ,
        cap.RUN_READ,
        cap.STATE_READ_METADATA,
        cap.VAR_READ,
        cap.CONFIG_READ,
        cap.RUN_TASK_READ,
        cap.NOTIFICATION_READ,
        cap.RUN_TRIGGER_READ,
    } == read
    assert cap.STATE_READ not in read and cap.RUN_PLAN not in read


def test_workspace_plan_tier_membership():
    plan = _ws("plan")
    # plan adds: queue plan-only, cancel/discard/retry, lock, raw state read,
    # drift dismiss. Raw state download == plan today (it contains secrets).
    assert {
        cap.RUN_PLAN,
        cap.RUN_CANCEL,
        cap.WORKSPACE_LOCK,
        cap.STATE_READ,
        cap.DRIFT_DISMISS,
    } <= plan
    assert cap.RUN_APPLY not in plan and cap.STATE_WRITE not in plan


def test_workspace_write_tier_membership():
    write = _ws("write")
    assert {
        cap.RUN_APPLY,
        cap.RUN_APPLY_DESTROY,
        cap.VAR_WRITE,
        cap.STATE_WRITE,
        cap.CONFIG_UPLOAD,
    } <= write
    assert cap.WORKSPACE_SETTINGS not in write and cap.WORKSPACE_DELETE not in write


def test_workspace_admin_tier_membership():
    admin = _ws("admin")
    # admin adds: settings, force-unlock, delete, delete-state-version, and the
    # three per-workspace resource managers (notification / run-task / run-trigger).
    assert {
        cap.WORKSPACE_SETTINGS,
        cap.WORKSPACE_FORCE_UNLOCK,
        cap.WORKSPACE_DELETE,
        cap.STATE_DELETE,
        cap.NOTIFICATION_MANAGE,
        cap.RUN_TASK_MANAGE,
        cap.RUN_TRIGGER_MANAGE,
    } <= admin
    # Variable SETS are platform-admin gated, not workspace-admin — they must NOT
    # leak into the workspace preset (this was a draft bug the gate survey caught).
    assert cap.PLATFORM_VARSET_ADMIN not in admin
    # No platform capability is ever in a label-scoped preset.
    assert not (admin & cap.PLATFORM_CAPABILITIES)


def test_other_axes_expand_and_are_cumulative():
    pool_admin = set(
        cap.expand_preset(
            workspace_permission=None,
            pool_permission="admin",
            registry_permission=None,
            catalog_permission=None,
        )
    )
    assert {cap.POOL_READ, cap.POOL_ASSIGN, cap.POOL_MANAGE} == pool_admin
    reg = set(
        cap.expand_preset(
            workspace_permission=None,
            pool_permission=None,
            registry_permission="write",
            catalog_permission=None,
        )
    )
    assert reg == {cap.REGISTRY_READ, cap.REGISTRY_WRITE}
    # catalog "none" grants nothing (opt-in axis, no floor)
    assert (
        cap.expand_preset(
            workspace_permission=None,
            pool_permission=None,
            registry_permission=None,
            catalog_permission="none",
        )
        == []
    )
    cat = set(
        cap.expand_preset(
            workspace_permission=None,
            pool_permission=None,
            registry_permission=None,
            catalog_permission="admin",
        )
    )
    assert cat == {cap.CATALOG_READ, cap.CATALOG_USE, cap.CATALOG_ADMIN}


def test_expand_is_total_and_sorted():
    # Unknown / None values contribute nothing — the migration must never throw.
    out = cap.expand_preset(
        workspace_permission="bogus",
        pool_permission=None,
        registry_permission="",
        catalog_permission="also-bogus",
    )
    assert out == []
    # Deterministic sorted output (stable on disk / in JSON).
    full = cap.expand_preset(
        workspace_permission="admin",
        pool_permission="admin",
        registry_permission="admin",
        catalog_permission="admin",
    )
    assert full == sorted(full)


def test_grantable_is_full_preset_union_and_platform_free():
    # The grantable set is exactly the union of every axis' top preset...
    full = set(
        cap.expand_preset(
            workspace_permission="admin",
            pool_permission="admin",
            registry_permission="admin",
            catalog_permission="admin",
        )
    )
    assert set(cap.GRANTABLE_CAPABILITIES) == full
    # ...and contains no platform:* token (those are #642, not yet grantable).
    assert not (cap.GRANTABLE_CAPABILITIES & cap.PLATFORM_CAPABILITIES)


def test_summarize_is_inverse_of_expand_for_every_preset_combo():
    # expand → summarize must round-trip to the original level tuple for every
    # combination of presets (the derived-summary read contract).
    ws_levels = ["read", "plan", "write", "admin"]
    other_levels = ["read", "write", "admin"]
    cat_levels = ["none", "read", "use", "admin"]
    for w in ws_levels:
        for p in other_levels:
            for r in other_levels:
                for c in cat_levels:
                    caps = cap.expand_preset(
                        workspace_permission=w,
                        pool_permission=p,
                        registry_permission=r,
                        catalog_permission=c,
                    )
                    summary = cap.summarize_capabilities(caps)
                    assert summary == {
                        "workspace_permission": w,
                        "pool_permission": p,
                        "registry_permission": r,
                        "catalog_permission": c,
                    }, f"round-trip failed for {(w, p, r, c)}: {summary}"


def test_summarize_reports_custom_for_granular_set():
    # A genuine subset that matches no preset must render "custom", not a preset.
    granular = [cap.RUN_READ, cap.VAR_READ]  # workspace read is a strict superset
    summary = cap.summarize_capabilities(granular)
    assert summary["workspace_permission"] == "custom"
    # Empty axes still resolve: catalog → "none", workspace (no empty preset) → custom.
    assert summary["catalog_permission"] == "none"
    assert summary["pool_permission"] == "none" or summary["pool_permission"] == "custom"


def test_normalize_preserves_unknown_and_dedups():
    # No aliases registered yet → identity (sorted, deduped).
    assert cap.normalize_capabilities([cap.RUN_READ, cap.RUN_READ, cap.VAR_READ]) == sorted(
        {cap.RUN_READ, cap.VAR_READ}
    )
    # Unknown tokens are preserved (never silently dropped — that would tighten).
    assert "future:unknown" in cap.normalize_capabilities([cap.RUN_READ, "future:unknown"])


def test_has_capability_is_membership():
    caps = frozenset(_ws("plan"))
    assert cap.has_capability(caps, cap.RUN_PLAN)
    assert not cap.has_capability(caps, cap.RUN_APPLY)


def test_builtin_capability_sets():
    admin = set(cap.capabilities_for_builtin("admin"))
    audit = set(cap.capabilities_for_builtin("audit"))
    everyone = set(cap.capabilities_for_builtin("everyone"))

    # admin = superuser: every grantable capability + every platform capability.
    assert cap.PLATFORM_CAPABILITIES <= admin
    assert cap.GRANTABLE_CAPABILITIES <= admin
    # execution-hooks management (#619/#673) is part of the platform vocabulary,
    # so admin's advertised capability set names it.
    assert cap.PLATFORM_HOOK_ADMIN in admin
    # audit = read-only everywhere + the (read-only) audit-log power; no
    # write/manage caps and no platform capability other than audit-admin.
    assert {cap.WORKSPACE_READ, cap.POOL_READ, cap.REGISTRY_READ, cap.CATALOG_READ} <= audit
    assert cap.PLATFORM_AUDIT_ADMIN in audit
    assert not (audit & {cap.STATE_WRITE, cap.VAR_WRITE, cap.RUN_APPLY, cap.WORKSPACE_DELETE})
    assert not (audit & (cap.PLATFORM_CAPABILITIES - {cap.PLATFORM_AUDIT_ADMIN}))
    assert cap.STATE_READ not in audit  # audit never downloads raw state
    # everyone = the read floor.
    assert everyone == _ws("read")
    assert cap.STATE_READ not in everyone


def test_caps_for_level_unions_across_axes():
    # A preset level unions every axis's capability set, so it is a faithful
    # stand-in for any single-axis resolver at that level (the test-mock oracle).
    write = cap.caps_for_level("write")
    assert cap.RUN_APPLY in write  # workspace axis write tier
    assert cap.VAR_WRITE in write  # workspace axis write tier
    assert cap.POOL_ASSIGN in write  # pool axis write tier
    assert cap.REGISTRY_WRITE in write  # registry axis
    assert cap.CATALOG_USE in write  # catalog axis (write → use)
    # It equals the union of the four single-axis expansions for that level.
    expected = set(
        cap.expand_preset(
            workspace_permission="write",
            pool_permission="write",
            registry_permission="write",
            catalog_permission="use",
        )
    )
    assert set(write) == expected
    # Strictly cumulative across the union: read ⊂ write.
    assert cap.caps_for_level("read") < write


def test_caps_for_level_use_is_catalog_only():
    # "use" is a catalog-only level: it grants catalog caps and nothing else.
    caps = cap.caps_for_level("use")
    assert cap.CATALOG_USE in caps
    assert not (caps & cap.axis_all_caps("workspace"))
    assert not (caps & cap.axis_all_caps("pool"))
    assert not (caps & cap.axis_all_caps("registry"))


def test_caps_for_level_none_and_unknown_are_empty():
    assert cap.caps_for_level("none") == frozenset()
    assert cap.caps_for_level(None) == frozenset()
    assert cap.caps_for_level("bogus") == frozenset()
    assert cap.caps_for_level("") == frozenset()


def test_capabilities_for_builtin_unknown_is_empty():
    assert cap.capabilities_for_builtin("nope") == []


def test_has_capability_empty_set_grants_nothing():
    assert cap.has_capability(frozenset(), cap.RUN_READ) is False
    assert cap.has_capability(frozenset(), cap.WORKSPACE_READ) is False
