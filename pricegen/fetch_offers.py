#!/usr/bin/env python3
"""Fetch each recipe's pricing offer from the official cloud API (#893).

Config-driven downloader for the scheduled publish job: for every entry in
``recipes.yaml`` it fetches that recipe's offer to the path the recipe expects
(``.cache/…``), so ``publish.py`` can then run over fresh data. All three
sources are official first-class APIs — no third-party pricing product:

* **AWS** — Price List Bulk API: the service's ``region_index.json`` → the
  region's ``index.json`` (public, no credentials).
* **Azure** — Retail Prices API, paginated via ``NextPageLink`` (public).
* **GCP** — Cloud Billing Catalog ``skus`` list, paginated (needs a free
  read-only API key in ``GCP_BILLING_API_KEY``).

Each ``recipes.yaml`` entry carries a ``fetch:`` block describing its source.
Run: ``python -m pricegen.fetch_offers`` (add ``--only aws`` to limit).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).parent
_UA = {"User-Agent": "terrapod-pricegen/0.1"}
_AWS_BULK = "https://pricing.us-east-1.amazonaws.com"


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(  # noqa: S310 — trusted pinned cloud hosts
        urllib.request.Request(url, headers=_UA)
    ) as resp:
        return json.load(resp)


def fetch_aws(fetch: dict) -> dict:
    """AWS Price List Bulk API: region_index → the region's index.json."""
    svc, region = fetch["service_code"], fetch["region"]
    idx = _get_json(f"{_AWS_BULK}/offers/v1.0/aws/{svc}/current/region_index.json")
    region_url = idx["regions"][region]["currentVersionUrl"]
    return _get_json(_AWS_BULK + region_url)


def fetch_azure(fetch: dict) -> dict:
    """Azure Retail Prices API — page through every Consumption item."""
    name, region = fetch["service_name"], fetch["region"]
    flt = (
        f"serviceName eq '{name}' and armRegionName eq '{region}' "
        "and priceType eq 'Consumption'"
    )
    url = "https://prices.azure.com/api/retail/prices?" + urllib.parse.urlencode(
        {"$filter": flt}
    )
    items: list[dict] = []
    while url:
        d = _get_json(url)
        items.extend(d.get("Items", []))
        url = d.get("NextPageLink")
    return {"Items": items}


def fetch_gcp(fetch: dict) -> dict:
    """GCP Cloud Billing Catalog — page through a service's SKUs (needs a key)."""
    key = os.environ.get("GCP_BILLING_API_KEY")
    if not key:
        raise SystemExit("GCP_BILLING_API_KEY not set — required to fetch GCP offers")
    svc = fetch["service_id"]
    skus: list[dict] = []
    tok = ""
    while True:
        url = (
            f"https://cloudbilling.googleapis.com/v1/services/{svc}/skus"
            f"?key={key}&pageSize=5000" + (f"&pageToken={tok}" if tok else "")
        )
        d = _get_json(url)
        skus.extend(d.get("skus", []))
        tok = d.get("nextPageToken")
        if not tok:
            break
    return {"skus": skus}


_FETCHERS = {"aws": fetch_aws, "azure": fetch_azure, "gcp": fetch_gcp}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=HERE / "recipes.yaml")
    ap.add_argument("--only", help="fetch only this provider (aws|azure|gcp)")
    args = ap.parse_args()

    config = yaml.safe_load(args.config.read_text())
    for cfg in config["recipes"]:
        provider = cfg["provider"]
        if args.only and provider != args.only:
            continue
        fetch = cfg.get("fetch")
        if not fetch:
            print(
                f"  {provider}/{cfg['recipe']}: no fetch block, skipping",
                file=sys.stderr,
            )
            continue
        out = cfg["offer"]
        out = Path(out) if Path(out).is_absolute() else HERE / out
        out.parent.mkdir(parents=True, exist_ok=True)
        print(f"  fetching {provider}/{cfg['recipe']} → {out} …", file=sys.stderr)
        offer = _FETCHERS[provider](fetch)
        size = len(
            offer.get("skus") or offer.get("Items") or offer.get("products") or {}
        )
        out.write_text(json.dumps(offer))
        print(f"    {size} items", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
