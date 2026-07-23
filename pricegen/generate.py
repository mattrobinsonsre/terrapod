#!/usr/bin/env python3
"""Generate one recipe's price rows from an official cloud price API (#893).

Provider-agnostic dev CLI: it picks a provider adapter (which normalizes that
cloud's feed into ``Unit``s), loads the provider's ``defaults.yaml`` + a resource
recipe, and runs the shared engine. The pricing DATA comes from the official
vendor API; the SKU->resource MAPPING is Terrapod's own original YAML.

Run from the repo root, to inspect a recipe's output + drift diagnostics:
    python3 -m pricegen.generate --provider aws --recipe aws_instance \\
        --offer pricegen/.cache/ec2-us-east-1.json

The rows print as a readable CSV table for inspection; the *published* pricesheet
is gzipped, normalized YAML, assembled by ``pricegen.publish`` — see #893.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib
import json
import sys
from pathlib import Path

import yaml

from pricegen import engine

HERE = Path(__file__).parent
CSV_HEADER = [
    "service",
    "product_family",
    "match_set",
    "pricing_match_set",
    "price",
    "price_type",
    "ccy",
]


def _manifest(stats: engine.RecipeStats, recipe: dict) -> dict:
    """The per-recipe drift-diagnostics record the scheduled generator persists
    and diffs across runs (#922). ``unmapped`` is the actionable signal: values
    the feed carried for a mapped field that our recipe didn't cover."""
    return {
        "resource_type": recipe["resource_type"],
        "service": recipe["service"],
        "rows": stats.rows,
        "units_in_family": stats.units_total,
        "skipped": {
            "select": stats.skipped_select,
            "absent": stats.skipped_absent,
            "unmapped": stats.skipped_unmapped,
        },
        "unmapped": {f: dict(c) for f, c in sorted(stats.unmapped.items())},
        "price_min": stats.price_min,
        "price_max": stats.price_max,
    }


def _report_stats(stats: engine.RecipeStats, rtype: str) -> None:
    print(f"=== diagnostics ({rtype}) ===", file=sys.stderr)
    print(
        f"  {stats.rows} rows from {stats.units_total} in-family units "
        f"(skipped: select={stats.skipped_select} absent={stats.skipped_absent} "
        f"unmapped={stats.skipped_unmapped})",
        file=sys.stderr,
    )
    if stats.price_min is not None:
        print(f"  price range: {stats.price_min} … {stats.price_max}", file=sys.stderr)
    if stats.unmapped:
        # The drift signal — a value the feed carries that our recipe drops.
        print("  UNMAPPED (recipe may need an update):", file=sys.stderr)
        for fieldname, counter in sorted(stats.unmapped.items()):
            for value, n in counter.most_common(10):
                print(f"    {fieldname}={value!r} ({n})", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", default="aws", help="aws | azure | gcp")
    ap.add_argument("--recipe", required=True)
    ap.add_argument("--offer", required=True, type=Path)
    ap.add_argument(
        "--out", type=Path, help="write rows here (gzip if .gz); default stdout"
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        help="write a drift-diagnostics manifest (JSON) here — the artifact the "
        "scheduled generator diffs across runs to alert on feed/logic drift (#922)",
    )
    args = ap.parse_args()

    pdir = HERE / "providers" / args.provider
    adapter = importlib.import_module(f"pricegen.providers.{args.provider}.adapter")
    defaults = yaml.safe_load((pdir / "defaults.yaml").read_text())
    rpath = Path(args.recipe)
    if not rpath.exists():
        rpath = pdir / "recipes" / f"{args.recipe}.yaml"
    recipe = yaml.safe_load(rpath.read_text())

    print(f"loading {args.provider} offer {args.offer} …", file=sys.stderr)
    offer = adapter.load(args.offer)
    units = adapter.iter_units(offer, term=defaults.get("price_term", "OnDemand"))
    stats = engine.RecipeStats()
    # A recipe is either `components:` (direct match, AWS/Azure) or `computed:`
    # (arithmetic assembly, GCP compute). generate_computed consumes the unit
    # stream once, so materialize it for that path.
    if "computed" in recipe:
        gen = engine.generate_computed(
            recipe, defaults, list(units), service=recipe["service"], stats=stats
        )
    else:
        gen = engine.generate(
            recipe, defaults, units, service=recipe["service"], stats=stats
        )
    rows = list(gen)
    print(f"generated {len(rows)} rows for {recipe['resource_type']}", file=sys.stderr)
    _report_stats(stats, recipe["resource_type"])
    if args.manifest:
        args.manifest.write_text(json.dumps(_manifest(stats, recipe), indent=2))
        print(f"wrote manifest {args.manifest}", file=sys.stderr)

    if args.out:
        opener = gzip.open if args.out.suffix == ".gz" else open
        with opener(args.out, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            w.writerows(rows)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        w = csv.writer(sys.stdout)
        w.writerow(CSV_HEADER)
        w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
