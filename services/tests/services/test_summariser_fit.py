"""Regression tests for plan-JSON fitting (#602).

The previous `_truncate_head` cut bytes from the tail, silently dropping the
end of `resource_changes` — so a destroy near the end of a large plan became
invisible and the AI summarised a plan it only half-read. `_fit_plan_json`
reduces structurally instead: every change keeps its address + actions; only
attribute detail is trimmed. These tests pin that guarantee.
"""

from __future__ import annotations

import json

from terrapod.services.summariser import _fit_plan_json


def _big_plan(n: int, *, delete_at: int) -> bytes:
    """A plan with ``n`` changes, each carrying a fat attribute body, and a
    `delete` at index ``delete_at`` (the rest are updates)."""
    rcs = []
    for i in range(n):
        actions = ["delete"] if i == delete_at else ["update"]
        rcs.append(
            {
                "address": f"aws_ssm_parameter.p{i}",
                "type": "aws_ssm_parameter",
                "name": f"p{i}",
                "change": {
                    "actions": actions,
                    "before": {"value": "x" * 400, "tags": {"k": "v" * 50}},
                    "after": {"value": "y" * 400, "tags": {"k": "v" * 50}},
                    "after_unknown": {"arn": True},
                },
            }
        )
    return json.dumps({"format_version": "1.2", "resource_changes": rcs}).encode("utf-8")


def test_under_cap_returns_everything_byte_identical():
    # When the plan fits, NOTHING is touched — not the changes, not the
    # background `configuration`/`planned_values` blocks. Byte-for-byte.
    plan = {
        "format_version": "1.2",
        "resource_changes": [
            {
                "address": "aws_db_instance.main",
                "type": "aws_db_instance",
                "name": "main",
                "change": {"actions": ["delete"], "before": {"engine": "postgres"}, "after": None},
            }
        ],
        "configuration": {"provider_config": {"aws": {"name": "aws"}}},
        "planned_values": {"root_module": {}},
    }
    data = json.dumps(plan).encode("utf-8")
    out = _fit_plan_json(data, 10_000_000)
    assert out == data.decode("utf-8")  # returned verbatim, untouched
    parsed = json.loads(out)
    assert "configuration" in parsed and "planned_values" in parsed  # nothing dropped


def _find_delete(plan: dict) -> dict | None:
    for r in plan["resource_changes"]:
        if r["change"]["actions"] == ["delete"]:
            return r
    return None


def test_tail_delete_survives_reduction():
    # ~1000 fat changes (~1MB) reduced to fit 200KB. The destroy is the LAST
    # entry — exactly what head-truncation dropped. The GUARANTEE: it's within
    # the cap, it's valid JSON, and the destroy is still present and named.
    n = 1000
    data = _big_plan(n, delete_at=n - 1)
    assert len(data) > 200_000  # genuinely over budget
    out = _fit_plan_json(data, 200_000)

    assert len(out.encode("utf-8")) <= 200_000  # within the cap
    plan = json.loads(out)  # still valid JSON
    d = _find_delete(plan)
    assert d is not None and d["address"] == f"aws_ssm_parameter.p{n - 1}"


def test_every_change_kept_when_attrs_trimmable():
    # At a generous cap, every change is kept (stage 3 just trims attributes).
    n = 600
    data = _big_plan(n, delete_at=300)
    out = _fit_plan_json(data, 500_000)
    plan = json.loads(out)
    addrs = {r["address"] for r in plan["resource_changes"]}
    assert addrs == {f"aws_ssm_parameter.p{i}" for i in range(n)}
    d = _find_delete(plan)
    assert d is not None and d["address"] == "aws_ssm_parameter.p300"


def test_extreme_destructive_always_kept():
    # Pathological: so many changes that even bare skeletons overflow the cap
    # (stage 4). The destroy MUST still be shown in full; the omitted routine
    # changes are counted, and none of them are destroys.
    n = 1000
    data = _big_plan(n, delete_at=n - 1)
    out = _fit_plan_json(data, 50_000)
    assert len(out.encode("utf-8")) <= 50_000
    plan = json.loads(out)
    rcs = plan["resource_changes"]
    deletes = [r for r in rcs if r["change"]["actions"] == ["delete"]]
    assert len(deletes) == 1  # the destroy survived
    assert deletes[0]["address"] == f"aws_ssm_parameter.p{n - 1}"
    assert len(rcs) < n  # some routine changes were omitted
    assert plan.get("_omitted_changes")  # ... and counted
    assert "delete" not in plan["_omitted_changes"]  # never a destroy


def test_unparseable_falls_back_safely():
    out = _fit_plan_json(b"{not valid json" + b"x" * 1000, 200)
    assert len(out) <= 250  # bounded, doesn't blow up


# ── the reduction must announce itself ──────────────────────────────────────


def _is_skeleton(rc: dict) -> bool:
    """True when this entry carries existence and action only — no attributes."""
    return set(rc) == {"address", "type", "name", "change"} and set(rc["change"]) == {"actions"}


def _cidr_plan(n: int, *, pad: int) -> bytes:
    """`n` updates that each open a security group to the world, with a fat
    attribute body so the fitter has to skeletonise them."""
    rcs = [
        {
            "address": f"aws_security_group.sg{i}",
            "type": "aws_security_group",
            "name": f"sg{i}",
            "mode": "managed",
            "provider_name": "registry.terraform.io/hashicorp/aws",
            "change": {
                "actions": ["update"],
                "before": {"cidr_blocks": ["10.0.0.0/8"]},
                "after": {"cidr_blocks": ["0.0.0.0/0"], "description": "x" * pad},
            },
        }
        for i in range(n)
    ]
    return json.dumps({"format_version": "1.2", "resource_changes": rcs}).encode("utf-8")


def test_a_skeletonised_plan_says_so_even_though_nothing_was_dropped():
    """The case with no `_omitted_changes` at all: every change is present, all
    of them reduced to address+actions. That reads as a complete plan, and the
    `0.0.0.0/0` in `change.after` is simply absent — so the model rated a plan it
    never saw in full, with nothing in the payload to tell it otherwise."""
    data = _cidr_plan(10, pad=2000)
    out = _fit_plan_json(data, 2_500)
    plan = json.loads(out)

    assert len(plan["resource_changes"]) == 10  # nothing dropped
    assert "_omitted_changes" not in plan  # ... so the old marker never fired
    assert all(_is_skeleton(r) for r in plan["resource_changes"])
    assert "0.0.0.0/0" not in out  # the offending value is genuinely gone

    assert plan["_reduced_changes"] == 10
    assert "_note" in plan, "a wholly skeletonised plan carried no truncation marker"


def test_the_marker_fires_on_a_partial_reduction_too():
    """Some entries full, some skeletons, nothing omitted — still partial."""
    data = _cidr_plan(40, pad=500)
    out = _fit_plan_json(data, 8_000)
    plan = json.loads(out)
    skeletons = [r for r in plan["resource_changes"] if _is_skeleton(r)]
    assert skeletons and len(skeletons) < 40  # genuinely a mixture
    assert "_omitted_changes" not in plan
    assert plan["_reduced_changes"] == len(skeletons)
    assert "_note" in plan


def test_a_full_plan_carries_no_marker():
    """The marker is worthless if it also appears when nothing was withheld."""
    out = _fit_plan_json(_cidr_plan(2, pad=10), 10_000_000)
    plan = json.loads(out)
    assert "_note" not in plan and "_reduced_changes" not in plan


def test_the_note_names_both_reduced_and_omitted():
    """The model has to be able to act on it: the note must name the keys that
    quantify what is missing, and say that a reduced change was not read."""
    data = _cidr_plan(10, pad=2000)
    plan = json.loads(_fit_plan_json(data, 2_500))
    note = plan["_note"]
    assert "_reduced_changes" in note and "_omitted_changes" in note
    assert "has NOT been read" in note


def _destroys_and_creates() -> bytes:
    """Two fat destroys and five slim creates: the second destroy cannot fit,
    while every create can."""

    def rc(addr: str, action: str, pad: str) -> dict:
        return {
            "address": addr,
            "type": "aws_db_instance",
            "name": addr.split(".")[1],
            "mode": "managed",
            "provider_name": "registry.terraform.io/hashicorp/aws",
            "change": {
                "actions": [action],
                "before": {"id": addr, "pad": pad},
                "after": None if action == "delete" else {"id": addr, "pad": pad},
            },
        }

    rcs = [rc("aws_db_instance.d0", "delete", "D" * 3000)]
    rcs.append(rc("aws_db_instance.d1", "delete", "D" * 3000))
    rcs += [rc(f"aws_db_instance.c{i}", "create", "C" * 200) for i in range(5)]
    return json.dumps({"format_version": "1.2", "resource_changes": rcs}).encode("utf-8")


def test_a_reduced_destroy_is_never_traded_for_a_full_create():
    """Walking the flat priority-ordered list greedily is not the same as
    honouring the priority order. A destroy too big for the remaining budget was
    skipped, and the smaller creates behind it then fit and spent it — so the
    model read every create's attributes in full and a destroy as address+actions
    only, which is the inversion the ordering exists to prevent."""
    data = _destroys_and_creates()
    plan = json.loads(_fit_plan_json(data, 6_000))
    by_addr = {r["address"]: r for r in plan["resource_changes"]}

    destroys_reduced = [
        a for a, r in by_addr.items() if a.startswith("aws_db_instance.d") and _is_skeleton(r)
    ]
    assert destroys_reduced, "fixture no longer exercises an unaffordable destroy"
    for i in range(5):
        assert _is_skeleton(by_addr[f"aws_db_instance.c{i}"]), (
            f"create c{i} was shown in full while destroy(s) {destroys_reduced} were reduced"
        )


def test_the_highest_priority_destroy_still_gets_its_full_body():
    """Reserving for the tier must not stop the destroys that DO fit."""
    plan = json.loads(_fit_plan_json(_destroys_and_creates(), 6_000))
    d0 = next(r for r in plan["resource_changes"] if r["address"] == "aws_db_instance.d0")
    assert not _is_skeleton(d0)
    assert d0["change"]["before"]["id"] == "aws_db_instance.d0"


def test_the_result_still_fits_the_cap_with_the_longer_note():
    """The note is reserved for, not hoped about — it is appended last and the
    cap is the contract."""
    for cap in (2_000, 2_500, 6_000, 50_000):
        out = _fit_plan_json(_cidr_plan(60, pad=900), cap)
        assert len(out.encode("utf-8")) <= cap, f"overflowed at cap={cap}"
        json.loads(out)  # and is still valid JSON
