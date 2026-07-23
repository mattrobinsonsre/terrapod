#!/usr/bin/env python3
"""Merge per-shard partial pricesheets into one published sheet (#1025).

The multi-region publish fans out across shards (one per AWS region + AWS-global
+ Azure + GCP — see ``pricegen.shard``); each shard runs ``pricegen.publish`` to
emit a *partial* ``prices.yaml.gz`` + ``manifest.json`` for its slice. This
combines them into the single artifacts the rolling release publishes:

* **sheet** — every partial's ``products`` concatenated (deduplicated: identical
  rows can't legitimately recur across region shards because ``region`` differs,
  but global/GCP rows are produced by exactly one shard, so dedup is only a
  defensive guard against an accidental double-run).
* **manifest** — the per-recipe drift records **aggregated by recipe** across the
  shards a recipe appears in (a regional recipe appears in every region shard):
  rows/units summed, ``unmapped`` counters merged, price range widened. The
  drift guardrail (``pricegen.drift``) keys on the recipe name and expects one
  record per recipe, so aggregation (not concatenation) is required.

Pure combine functions (``merge_sheets`` / ``merge_manifests``) so they unit-test
without any shard artifacts; ``main`` wires in file globbing + gzip I/O.
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
from pathlib import Path

import yaml

from pricegen.publish import SCHEMA


def merge_sheets(sheets: list[dict]) -> dict:
    """Concatenate partial sheets' products into one sheet (dedup identical)."""
    products: list[dict] = []
    seen: set = set()
    currency = "USD"
    for sheet in sheets:
        currency = sheet.get("currency", currency)
        for p in sheet.get("products", []):
            # A product's identity is its full tuple; match/pricing are the two
            # multi-valued fields — sort so key order can't spuriously differ.
            key = (
                p["service"],
                p["family"],
                "&".join(sorted(p["match"].split("&"))),
                "&".join(sorted(p["pricing"].split("&"))),
                p["price"],
                p["price_type"],
            )
            if key in seen:
                continue
            seen.add(key)
            products.append(p)
    return {"schema": SCHEMA, "currency": currency, "products": products}


def merge_manifests(manifests: list[dict]) -> dict:
    """Aggregate partial manifests' per-recipe drift records by recipe name."""
    agg: dict[str, dict] = {}
    for manifest in manifests:
        for r in manifest.get("recipes", []):
            cur = agg.get(r["recipe"])
            if cur is None:
                # Deep-ish copy: we mutate counters/skipped below.
                agg[r["recipe"]] = {
                    **r,
                    "skipped": dict(r.get("skipped", {})),
                    "unmapped": {f: dict(c) for f, c in r.get("unmapped", {}).items()},
                }
                continue
            cur["rows"] += r.get("rows", 0)
            cur["units_in_family"] += r.get("units_in_family", 0)
            for k, v in r.get("skipped", {}).items():
                cur["skipped"][k] = cur["skipped"].get(k, 0) + v
            for f, counts in r.get("unmapped", {}).items():
                dst = cur["unmapped"].setdefault(f, {})
                for value, n in counts.items():
                    dst[value] = dst.get(value, 0) + n
            for key, op in (("price_min", min), ("price_max", max)):
                a, b = cur.get(key), r.get(key)
                cur[key] = op(a, b) if a is not None and b is not None else (a or b)
    recipes = sorted(agg.values(), key=lambda r: (r["provider"], r["recipe"]))
    return {
        "schema": SCHEMA,
        "total_products": sum(r["rows"] for r in recipes),
        "recipes": recipes,
    }


def _load_yaml_gz(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sheets", required=True, help="glob for partial prices.yaml(.gz) files"
    )
    ap.add_argument(
        "--manifests", required=True, help="glob for partial manifest.json files"
    )
    ap.add_argument("--out", type=Path, default=Path("prices.yaml.gz"))
    ap.add_argument("--manifest", type=Path, default=Path("manifest.json"))
    args = ap.parse_args()

    sheet_paths = sorted(Path(p) for p in glob.glob(args.sheets))
    manifest_paths = sorted(Path(p) for p in glob.glob(args.manifests))
    if not sheet_paths:
        raise SystemExit(f"no partial sheets matched {args.sheets!r}")
    print(
        f"merging {len(sheet_paths)} partial sheets + {len(manifest_paths)} manifests",
        file=sys.stderr,
    )

    sheet = merge_sheets([_load_yaml_gz(p) for p in sheet_paths])
    manifest = merge_manifests([json.loads(p.read_text()) for p in manifest_paths])

    with gzip.open(args.out, "wt") as f:
        yaml.safe_dump(sheet, f, sort_keys=False)
    args.manifest.write_text(json.dumps(manifest, indent=2))
    print(
        f"wrote {args.out} ({len(sheet['products'])} products across "
        f"{len(manifest['recipes'])} recipes) + {args.manifest}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
