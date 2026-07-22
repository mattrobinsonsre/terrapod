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
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).parent
_UA = {"User-Agent": "terrapod-pricegen/0.1"}
_AWS_BULK = "https://pricing.us-east-1.amazonaws.com"
# Within one run, a large offer (e.g. AmazonEC2) is downloaded once and reused
# across recipes that read different families of it (aws_instance + aws_ebs_volume).
_BIG_OFFER_CACHE: dict[str, str] = {}


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(  # noqa: S310 — trusted pinned cloud hosts
        urllib.request.Request(url, headers=_UA)
    ) as resp:
        return json.load(resp)


def _keep(prod: dict, families: set, keep_attrs: dict) -> bool:
    """A product is kept if its family is wanted and every ``keep_attrs`` filter
    passes: a filter matches when the attribute is ABSENT (mirrors the recipe's
    canonical "apply only where present") or its value is in the allowed set."""
    if prod.get("productFamily") not in families:
        return False
    attrs = prod.get("attributes", {})
    for key, allowed in keep_attrs.items():
        v = attrs.get(key)
        if v is not None and v not in allowed:
            return False
    return True


def _stream_filter_aws(region_url: str, families: list, keep_attrs: dict) -> dict:
    """Stream a large AWS offer with ijson, keeping only products in ``families``
    that pass ``keep_attrs`` (and their OnDemand terms) — so a 450 MB offer never
    lands in RAM. The offer is streamed to a temp file (constant memory), then
    parsed incrementally; only the small filtered subset is held. Applying the
    recipe's canonical/select as ``keep_attrs`` here keeps that subset tiny
    (~2k products for EC2 instances vs ~106k) so ``generate`` stays light too."""
    import ijson  # heavy dep, only needed for the big-offer path

    fams = set(families)
    keep = {k: set(v) for k, v in keep_attrs.items()}
    tmp = _BIG_OFFER_CACHE.get(region_url)
    if tmp is None:
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with (
            urllib.request.urlopen(  # noqa: S310 — trusted pinned host
                urllib.request.Request(region_url, headers=_UA)
            ) as resp,
            open(tmp, "wb") as out,
        ):
            shutil.copyfileobj(resp, out)  # stream to disk, low RAM
        _BIG_OFFER_CACHE[region_url] = tmp
    products: dict = {}
    with open(tmp, "rb") as f:
        for sku, prod in ijson.kvitems(f, "products"):
            if _keep(prod, fams, keep):
                products[sku] = prod
    terms: dict = {}
    with open(tmp, "rb") as f:
        for sku, term in ijson.kvitems(f, "terms.OnDemand"):
            if sku in products:
                terms[sku] = term
    return {"products": products, "terms": {"OnDemand": terms}}


def _cleanup_big_offers() -> None:
    """Remove the cached big-offer temp files at end of run."""
    for path in _BIG_OFFER_CACHE.values():
        try:
            os.unlink(path)
        except OSError:
            pass
    _BIG_OFFER_CACHE.clear()


def fetch_aws(fetch: dict) -> dict:
    """AWS Price List Bulk API: region_index → the region's offer.

    Small offers load whole. Large ones (a ``families`` filter in the fetch
    block, e.g. AmazonEC2) are stream-filtered with ijson to just the needed
    families + ``keep_attrs``, so RAM stays flat regardless of offer size."""
    svc, region = fetch["service_code"], fetch["region"]
    idx = _get_json(f"{_AWS_BULK}/offers/v1.0/aws/{svc}/current/region_index.json")
    region_url = _AWS_BULK + idx["regions"][region]["currentVersionUrl"]
    families = fetch.get("families")
    if families:
        return _stream_filter_aws(region_url, families, fetch.get("keep_attrs", {}))
    return _get_json(region_url)


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
    try:
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
    finally:
        _cleanup_big_offers()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
