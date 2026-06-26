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
