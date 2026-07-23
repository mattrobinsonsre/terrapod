"""Unit tests for the partial-sheet merge (#1025 multi-region).

`merge_sheets` / `merge_manifests` are pure — they combine the per-shard partial
artifacts into the single published sheet + aggregated drift manifest.
"""

from __future__ import annotations

from pricegen.merge import merge_manifests, merge_sheets
from pricegen.publish import SCHEMA


def _product(
    region, price="0.1", match="type=aws_instance&values.instance_type=t3.micro"
):
    return {
        "service": "AmazonEC2",
        "family": "Compute Instance",
        "match": match,
        "pricing": f"region={region}&service_class=instance",
        "price": price,
        "price_type": "t",
    }


def test_merge_sheets_concatenates_regions():
    a = {"schema": SCHEMA, "currency": "USD", "products": [_product("us-east-1")]}
    b = {"schema": SCHEMA, "currency": "USD", "products": [_product("eu-west-1")]}
    out = merge_sheets([a, b])
    assert out["schema"] == SCHEMA and out["currency"] == "USD"
    regions = {p["pricing"].split("region=")[1].split("&")[0] for p in out["products"]}
    assert regions == {"us-east-1", "eu-west-1"}


def test_merge_sheets_dedups_identical_rows():
    # Same row from two shards (e.g. an accidental double-run) collapses to one.
    a = {"products": [_product("us-east-1")]}
    b = {"products": [_product("us-east-1")]}
    assert len(merge_sheets([a, b])["products"]) == 1


def test_merge_sheets_dedup_is_order_insensitive_on_match():
    # match/pricing key order must not defeat dedup.
    a = {"products": [_product("us-east-1", match="type=aws_instance&values.x=1")]}
    b = {"products": [_product("us-east-1", match="values.x=1&type=aws_instance")]}
    assert len(merge_sheets([a, b])["products"]) == 1


def _manifest(recipe, rows, provider="aws", **kw):
    return {
        "recipes": [
            {
                "provider": provider,
                "recipe": recipe,
                "resource_type": kw.get("rtype", recipe),
                "rows": rows,
                "units_in_family": kw.get("units", rows),
                "skipped": kw.get("skipped", {"select": 0, "absent": 0, "unmapped": 0}),
                "unmapped": kw.get("unmapped", {}),
                "price_min": kw.get("pmin"),
                "price_max": kw.get("pmax"),
            }
        ]
    }


def test_merge_manifests_aggregates_recipe_across_regions():
    # aws_instance appears in two region shards -> one aggregated record.
    m1 = _manifest("aws_instance", 100, units=200, pmin=0.01, pmax=5.0)
    m2 = _manifest("aws_instance", 120, units=240, pmin=0.02, pmax=6.0)
    out = merge_manifests([m1, m2])
    assert len(out["recipes"]) == 1
    r = out["recipes"][0]
    assert r["recipe"] == "aws_instance"
    assert r["rows"] == 220 and r["units_in_family"] == 440
    assert r["price_min"] == 0.01 and r["price_max"] == 6.0
    assert out["total_products"] == 220


def test_merge_manifests_merges_unmapped_counters():
    m1 = _manifest("aws_db_instance", 10, unmapped={"match.engine": {"newsql": 2}})
    m2 = _manifest("aws_db_instance", 12, unmapped={"match.engine": {"newsql": 3}})
    out = merge_manifests([m1, m2])
    assert out["recipes"][0]["unmapped"]["match.engine"]["newsql"] == 5


def test_merge_manifests_distinct_recipes_kept_separate():
    out = merge_manifests(
        [
            _manifest("aws_instance", 5),
            _manifest("google_compute_disk", 3, provider="gcp"),
        ]
    )
    assert {r["recipe"] for r in out["recipes"]} == {
        "aws_instance",
        "google_compute_disk",
    }
    # sorted by (provider, recipe): aws before gcp
    assert out["recipes"][0]["provider"] == "aws"
