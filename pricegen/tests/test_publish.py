"""Unit tests for the publish combiner (#893).

`combine` is pure — it takes per-recipe (config, rows, stats) and returns the
YAML sheet dict + the aggregated drift manifest — so it's tested with synthetic
data, no offer files.
"""

from __future__ import annotations

from pricegen.engine import RecipeStats
from pricegen.publish import SCHEMA, combine


def _row(service, rtype, price, ptype="t"):
    return (
        service,
        "Fam",
        f"type={rtype}&values.x=1",
        "region=us-east-1&service_class=instance",
        price,
        ptype,
        "USD",
    )


def _stats(rows, **kw):
    s = RecipeStats()
    s.rows = rows
    s.units_total = kw.get("units", rows)
    s.price_min = kw.get("pmin")
    s.price_max = kw.get("pmax")
    for f, vals in kw.get("unmapped", {}).items():
        for v, n in vals.items():
            for _ in range(n):
                s._note_unmapped(f, v)
    return s


def test_combine_aggregates_products_and_metadata():
    results = [
        (
            {"provider": "aws", "recipe": "r1"},
            [_row("AmazonEC2", "aws_instance", "0.1")],
            _stats(1, pmin=0.1, pmax=0.1),
        ),
        (
            {"provider": "gcp", "recipe": "r2"},
            [_row("Compute Engine", "google_compute_instance", "0.2")],
            _stats(1, pmin=0.2, pmax=0.2),
        ),
    ]
    sheet, manifest = combine(results)
    assert sheet["schema"] == SCHEMA and sheet["currency"] == "USD"
    assert len(sheet["products"]) == 2
    p = sheet["products"][0]
    assert set(p) == {"service", "family", "match", "pricing", "price", "price_type"}
    assert p["service"] == "AmazonEC2" and p["price"] == "0.1"
    assert manifest["total_products"] == 2
    assert {r["provider"] for r in manifest["recipes"]} == {"aws", "gcp"}
    assert manifest["recipes"][0]["resource_type"] == "aws_instance"


def test_combine_surfaces_unmapped_drift_in_manifest():
    stats = _stats(
        1, pmin=1.0, pmax=1.0, unmapped={"match.engine": {"Db2|Standard": 3}}
    )
    _, manifest = combine(
        [
            (
                {"provider": "aws", "recipe": "r"},
                [_row("S", "aws_db_instance", "1.0")],
                stats,
            )
        ]
    )
    rec = manifest["recipes"][0]
    assert rec["skipped"]["unmapped"] == 3
    assert rec["unmapped"]["match.engine"]["Db2|Standard"] == 3


def test_combine_handles_empty_recipe_rows():
    # a recipe that produced zero rows (e.g. feed collapse) still records a
    # manifest entry — that's exactly the drift signal the guardrail acts on.
    _, manifest = combine([({"provider": "aws", "recipe": "r"}, [], _stats(0))])
    assert manifest["total_products"] == 0
    assert manifest["recipes"][0]["rows"] == 0
