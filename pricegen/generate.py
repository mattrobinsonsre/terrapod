#!/usr/bin/env python3
"""Generate an OpenInfraQuote-shaped price sheet from an official cloud price API (#893).

Provider-agnostic CLI: it picks a provider adapter (which normalizes that cloud's
feed into ``Unit``s), loads the provider's ``defaults.yaml`` + a resource recipe,
and runs the shared engine. The pricing DATA comes from the official vendor API;
the SKU->resource MAPPING is Terrapod's own original YAML. No third-party pricing
product/data/code is involved.

Run from the repo root:
    python3 -m pricegen.generate --provider aws --recipe aws_instance \\
        --offer pricegen/.cache/ec2-us-east-1.json --compare-oiq us-east-1

Output is CSV here only to row-parity-diff against oiq's ``prices.csv`` (AWS
only — oiq doesn't cover Azure/GCP); the published format is gzipped, normalized
YAML — see #893.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib
import json
import sys
import urllib.request
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


def _normalize(row: list[str]) -> tuple:
    service, fam, ms, pms, price, ptype, ccy = row
    return (
        service,
        fam,
        frozenset(ms.split("&")),
        frozenset(pms.split("&")),
        round(float(price), 6),
        ptype,
        ccy,
    )


def fetch_oiq_rows(rtype: str, region: str) -> list[list[str]]:
    req = urllib.request.Request(
        "https://oiq.terrateam.io/prices.csv.gz",
        headers={"User-Agent": "terrapod-pricegen/0.1"},
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - trusted pinned host
        data = gzip.decompress(resp.read())
    out = []
    for line in data.decode().splitlines():
        row = next(csv.reader([line]))
        if (
            len(row) == 7
            and f"type={rtype}" in row[2]
            and f"region={region}" in row[3].split("&")
        ):
            out.append(row)
    return out


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
    ap.add_argument("--compare-oiq", metavar="REGION")
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
    elif not args.compare_oiq:
        w = csv.writer(sys.stdout)
        w.writerow(CSV_HEADER)
        w.writerows(rows)

    if args.compare_oiq:
        region = args.compare_oiq
        oiq = fetch_oiq_rows(recipe["resource_type"], region)
        ours = {_normalize(r) for r in rows if f"region={region}" in r[3].split("&")}
        theirs = {_normalize(r) for r in oiq}
        only_ours, only_theirs, both = ours - theirs, theirs - ours, ours & theirs
        print(
            f"\n=== row-parity vs oiq ({recipe['resource_type']}, {region}) ===",
            file=sys.stderr,
        )
        print(f"  match:     {len(both)}", file=sys.stderr)
        print(f"  only ours: {len(only_ours)}", file=sys.stderr)
        print(f"  only oiq:  {len(only_theirs)}", file=sys.stderr)
        for label, s in (("only ours", only_ours), ("only oiq", only_theirs)):
            for item in list(s)[:6]:
                print(
                    f"    [{label}] {sorted(item[2])} | {item[4]} {item[5]}",
                    file=sys.stderr,
                )
        return 0 if not only_ours and not only_theirs else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
