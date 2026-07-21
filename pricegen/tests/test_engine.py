"""Unit tests for the shared recipe engine (#893).

Deterministic — they build ``Unit``s directly, no cloud feed. They cover the
pure logic: field resolution + miss classification, select filtering, dedup,
tier bounds, and the drift-diagnostics counters (#922). The *drift guardrail*
itself (diffing manifests across scheduled runs, opening an issue) is a property
of the scheduled publish workflow, not of this engine — these only assert that
the engine emits the right signal for that workflow to act on.
"""

from __future__ import annotations

from pricegen.engine import RecipeStats, _resolve2, generate
from pricegen.models import Price, Unit


# --- field resolution + miss classification --------------------------------


def test_resolve_literal():
    assert _resolve2("linux", {}) == ("linux", None, None)


def test_resolve_attr_present_and_absent():
    assert _resolve2({"attr": "instanceType"}, {"instanceType": "m5.large"}) == (
        "m5.large",
        None,
        None,
    )
    assert _resolve2({"attr": "instanceType"}, {}) == (None, "absent", None)


def test_resolve_map_hit_and_unmapped():
    spec = {"attr": "operatingSystem", "map": {"Linux": "linux"}}
    assert _resolve2(spec, {"operatingSystem": "Linux"}) == ("linux", None, None)
    # present but not in the map -> the actionable drift signal, carries value.
    assert _resolve2(spec, {"operatingSystem": "Haiku"}) == (None, "unmapped", "Haiku")
    # attr absent -> expected, not drift.
    assert _resolve2(spec, {}) == (None, "absent", None)


def test_resolve_regex_extract_and_miss():
    spec = {"attr": "meterName", "regex": r"^(\w+) IOPS"}
    assert _resolve2(spec, {"meterName": "gp3 IOPS charge"}) == ("gp3", None, None)
    # string present but pattern didn't match -> unmapped, carries the raw value.
    assert _resolve2(spec, {"meterName": "flat rate"}) == (
        None,
        "unmapped",
        "flat rate",
    )


def test_resolve_from_composite_map():
    spec = {
        "from": ["databaseEngine", "databaseEdition"],
        "map": {"SQL Server|Enterprise": "sqlserver-ee"},
    }
    attrs = {"databaseEngine": "SQL Server", "databaseEdition": "Enterprise"}
    assert _resolve2(spec, attrs) == ("sqlserver-ee", None, None)
    # a real combo we didn't map -> unmapped, carries the joined key.
    assert _resolve2(
        spec, {"databaseEngine": "Db2", "databaseEdition": "Standard"}
    ) == (
        None,
        "unmapped",
        "Db2|Standard",
    )
    # nothing present -> absent.
    assert _resolve2(spec, {}) == (None, "absent", None)


# --- generate: rows, dedup, diagnostics ------------------------------------


def _recipe():
    return {
        "resource_type": "aws_instance",
        "service": "AmazonEC2",
        "components": [
            {
                "product_family": "^Compute Instance",
                "service_class": "instance",
                "match": {"instance_type": {"attr": "instanceType"}},
                "select": {},
                "pricing": {
                    "os": {"attr": "operatingSystem", "map": {"Linux": "linux"}}
                },
                "price": {"type": "t", "unit": "Hrs"},
                "tier": "usage",
            }
        ],
    }


def _defaults():
    return {"pricing_common": {"region": "us-east-1"}, "canonical": {}}


def _unit(itype, os="Linux", usd="0.10"):
    return Unit(
        family="Compute Instance",
        attrs={"instanceType": itype, "operatingSystem": os},
        prices=[Price(usd=usd, unit="Hrs")],
    )


def test_generate_emits_expected_row():
    stats = RecipeStats()
    rows = list(
        generate(
            _recipe(),
            _defaults(),
            [_unit("m5.large")],
            service="AmazonEC2",
            stats=stats,
        )
    )
    assert len(rows) == 1
    service, fam, ms, pms, price, ptype, ccy = rows[0]
    assert ms == "type=aws_instance&values.instance_type=m5.large"
    assert "os=linux" in pms and "region=us-east-1" in pms
    assert "start_usage_amount=0" in pms and "end_usage_amount=Inf" in pms
    assert price == "0.10" and ptype == "t" and ccy == "USD"
    assert stats.rows == 1 and stats.units_total == 1


def test_generate_dedups_identical_units():
    # two feed SKUs, same coordinates + price -> one row (AWS carries dupes).
    units = [_unit("m5.large"), _unit("m5.large")]
    stats = RecipeStats()
    rows = list(
        generate(_recipe(), _defaults(), units, service="AmazonEC2", stats=stats)
    )
    assert len(rows) == 1
    assert stats.units_total == 2 and stats.rows == 1


def test_generate_records_unmapped_pricing_value_as_drift():
    # an OS the pricing map doesn't cover -> dropped, surfaced as drift signal.
    stats = RecipeStats()
    rows = list(
        generate(
            _recipe(),
            _defaults(),
            [_unit("m5.large", os="Plan9")],
            service="AmazonEC2",
            stats=stats,
        )
    )
    assert rows == []
    assert stats.skipped_unmapped == 1
    assert stats.unmapped["pricing.os"]["Plan9"] == 1


def test_generate_price_range_tracked():
    units = [_unit("a", usd="0.05"), _unit("b", usd="0.40")]
    stats = RecipeStats()
    list(generate(_recipe(), _defaults(), units, service="AmazonEC2", stats=stats))
    assert stats.price_min == 0.05 and stats.price_max == 0.40


def test_generate_select_filter_counts_separately():
    recipe = _recipe()
    recipe["components"][0]["select"] = {"operatingSystem": ["Linux"]}
    stats = RecipeStats()
    units = [_unit("m5.large", os="Linux"), _unit("m5.large", os="Windows")]
    rows = list(generate(recipe, _defaults(), units, service="AmazonEC2", stats=stats))
    assert len(rows) == 1  # only the Linux unit
    assert stats.skipped_select == 1  # the Windows unit, via select not unmapped


def test_generate_tier_bounds_override():
    recipe = _recipe()
    comp = recipe["components"][0]
    comp["tier"] = "provision"
    comp["price"] = {"type": "o", "unit": "Hrs"}
    comp["tier_bounds"] = [
        {"when": {"instanceType": "m5.large"}, "start": "3000", "end": "Inf"}
    ]
    rows = list(generate(recipe, _defaults(), [_unit("m5.large")], service="AmazonEC2"))
    assert "start_provision_amount=3000" in rows[0][3]
    assert "end_provision_amount=Inf" in rows[0][3]
