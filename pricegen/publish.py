#!/usr/bin/env python3
"""Combine every recipe into one published Terrapod pricesheet (#893).

Runs each entry in ``recipes.yaml`` through the engine, then writes two
artifacts:

* ``prices.yaml.gz`` — the combined pricesheet in Terrapod's own **YAML** schema
  (``schema: terrapod-pricesheet/v1``), gzipped. This is what a Terrapod instance
  consumes (via ``cost_estimation.prices_url``), replacing the dependency on
  OpenInfraQuote's hosted CSV. YAML (not CSV) because it is self-describing,
  versioned, and extensible; gzip because the row set, while far smaller and more
  targeted than a 200k-row flat CSV, still compresses well.
* ``manifest.json`` — the aggregated per-recipe **drift diagnostics** (row
  counts, price ranges, and the unmapped-value signal). The scheduled publish
  workflow diffs this against the last published manifest to detect that a cloud
  changed its API and a recipe needs updating, and refuses to publish a
  collapsed sheet over a good one (#922).

The ``combine`` function is pure (results -> sheet + manifest) so it is unit
tested without any offer files; ``main`` wires in offer loading + the engine.
"""

from __future__ import annotations

import argparse
import gzip
import importlib
import json
import sys
from pathlib import Path

import yaml

from pricegen import engine

HERE = Path(__file__).parent
SCHEMA = "terrapod-pricesheet/v1"


def _row_to_product(row: tuple) -> dict:
    service, family, match_set, pricing_match_set, price, price_type, _ccy = row
    return {
        "service": service,
        "family": family,
        "match": match_set,
        "pricing": pricing_match_set,
        "price": price,
        "price_type": price_type,
    }


def combine(
    results: list[tuple[dict, list[tuple], engine.RecipeStats]],
) -> tuple[dict, dict]:
    """Combine per-recipe (config, rows, stats) into (sheet_dict, manifest_dict).

    Pure — no I/O. ``results`` is one tuple per recipe. Currency is assumed USD
    (every adapter emits USD today); a mixed-currency sheet would carry it
    per-product instead.
    """
    products: list[dict] = []
    recipes: list[dict] = []
    for cfg, rows, stats in results:
        products.extend(_row_to_product(r) for r in rows)
        recipes.append(
            {
                "provider": cfg["provider"],
                "recipe": cfg["recipe"],
                "resource_type": rows[0][2].split("&", 1)[0].removeprefix("type=")
                if rows
                else cfg["recipe"],
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
        )
    sheet = {"schema": SCHEMA, "currency": "USD", "products": products}
    manifest = {
        "schema": SCHEMA,
        "total_products": len(products),
        "recipes": recipes,
    }
    return sheet, manifest


def _run_one(cfg: dict) -> tuple[dict, list[tuple], engine.RecipeStats]:
    pdir = HERE / "providers" / cfg["provider"]
    adapter = importlib.import_module(f"pricegen.providers.{cfg['provider']}.adapter")
    defaults = yaml.safe_load((pdir / "defaults.yaml").read_text())
    recipe = yaml.safe_load((pdir / "recipes" / f"{cfg['recipe']}.yaml").read_text())
    offer_path = cfg["offer"]
    if not Path(offer_path).is_absolute():
        offer_path = HERE / offer_path
    offer = adapter.load(offer_path)
    units = adapter.iter_units(offer, term=defaults.get("price_term", "OnDemand"))
    stats = engine.RecipeStats()
    if "computed" in recipe:
        rows = list(
            engine.generate_computed(
                recipe, defaults, list(units), service=recipe["service"], stats=stats
            )
        )
    else:
        rows = list(
            engine.generate(
                recipe, defaults, units, service=recipe["service"], stats=stats
            )
        )
    print(f"  {cfg['provider']}/{cfg['recipe']}: {stats.rows} rows", file=sys.stderr)
    return cfg, rows, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=HERE / "recipes.yaml")
    ap.add_argument("--out", type=Path, default=Path("prices.yaml.gz"))
    ap.add_argument("--manifest", type=Path, default=Path("manifest.json"))
    args = ap.parse_args()

    config = yaml.safe_load(args.config.read_text())
    results = [_run_one(cfg) for cfg in config["recipes"]]
    sheet, manifest = combine(results)

    with gzip.open(args.out, "wt") as f:
        yaml.safe_dump(sheet, f, sort_keys=False)
    args.manifest.write_text(json.dumps(manifest, indent=2))
    print(
        f"wrote {args.out} ({manifest['total_products']} products) + {args.manifest}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
