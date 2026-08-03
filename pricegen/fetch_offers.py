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
import time
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from pricegen.shard import ALL, add_shard_args, matches, regions_for

HERE = Path(__file__).parent
_UA = {"User-Agent": "terrapod-pricegen/0.1"}
_AWS_BULK = "https://pricing.us-east-1.amazonaws.com"
# Within one run, a large offer (e.g. AmazonEC2) is downloaded once and reused
# across recipes that read different families of it (aws_instance + aws_ebs_volume).
_BIG_OFFER_CACHE: dict[str, str] = {}


#: Bounded retry for the offer downloads. These are large files (an EC2 region
#: offer is hundreds of MB) pulled from public cloud endpoints, and a single
#: transient reset used to fail the whole shard — and with it the fan-out and
#: the weekly publish. Seen live: the 2026-07-27 run died on
#: `ConnectionReset [Errno 104]` mid-TLS-handshake for one of ~36 regions.
_RETRIES = 4
_BACKOFF = 3.0  # seconds, doubled per attempt: 3, 6, 12


def _urlopen_retrying(url: str, *, timeout: int = 300):
    """`urlopen` with bounded backoff on transient failures.

    Retries connection errors, timeouts, and 5xx/429. A definitive 4xx is a
    real answer — a wrong URL or a withdrawn offer — so it raises immediately
    rather than burning four attempts on it.
    """
    req = urllib.request.Request(url, headers=_UA)
    for attempt in range(_RETRIES):
        try:
            return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — pinned cloud hosts
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last: Exception = e
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            ssl.SSLError,
        ) as e:
            last = e
        if attempt == _RETRIES - 1:
            raise RuntimeError(
                f"giving up on {url} after {_RETRIES} attempts: {last!r}"
            ) from last
        delay = _BACKOFF * (2**attempt)
        print(
            f"  transient fetch failure ({last!r}); retrying in {delay:.0f}s "
            f"[{attempt + 1}/{_RETRIES - 1}]",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
    raise AssertionError("unreachable")


def _get_json(url: str) -> dict:
    with _urlopen_retrying(url) as resp:
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
            _urlopen_retrying(region_url) as resp,
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


def fetch_aws(fetch: dict, regions: list[str]) -> dict:
    """AWS Price List Bulk API: region_index → each region's offer, merged (#1025).

    Loops ``regions``, downloading each region's offer and merging the products +
    OnDemand terms into one offer dict. AWS SKU ids are region-specific, so there
    are no key collisions; each product carries its own ``regionCode``, so the
    emitted rows are correctly per-region without any generate-side change.

    Small offers load whole. Large ones (a ``families`` filter in the fetch block,
    e.g. AmazonEC2) are stream-filtered with ijson per region to just the needed
    families + ``keep_attrs``, so RAM stays flat regardless of offer size — and
    the per-region download is cached, so a region's 450 MB is pulled once and
    reused across every recipe reading that family."""
    svc = fetch["service_code"]
    # A few services (Route53, CloudFront) price globally — their products aren't
    # region-split, so there is no region_index; the whole offer lives at
    # current/index.json. `global: true` fetches that instead of any region.
    if fetch.get("global"):
        return _get_json(f"{_AWS_BULK}/offers/v1.0/aws/{svc}/current/index.json")
    idx = _get_json(f"{_AWS_BULK}/offers/v1.0/aws/{svc}/current/region_index.json")
    if regions == ALL:  # discover every region this service is offered in (#1025)
        regions = list(idx["regions"])
    families = fetch.get("families")
    keep_attrs = fetch.get("keep_attrs", {})
    products: dict = {}
    terms: dict = {}
    for region in regions:
        entry = idx["regions"].get(region)
        if entry is None:
            # Not every service is offered in every region — skip cleanly.
            print(f"    {svc}: no offer for region {region}, skipping", file=sys.stderr)
            continue
        region_url = _AWS_BULK + entry["currentVersionUrl"]
        if families:
            sub = _stream_filter_aws(region_url, families, keep_attrs)
        else:
            sub = _get_json(region_url)
        products.update(sub.get("products", {}))
        terms.update(sub.get("terms", {}).get("OnDemand", {}))
    return {"products": products, "terms": {"OnDemand": terms}}


def fetch_azure(fetch: dict, regions: list[str]) -> dict:
    """Azure Retail Prices API — page through every Consumption item, per region.

    Loops ``regions`` (#1025), filtering each with ``armRegionName eq '<region>'``
    and concatenating the ``Items``. Each item carries its own ``armRegionName``,
    so the emitted rows are per-region with no generate-side change. The
    :data:`~pricegen.shard.ALL` sentinel drops the region filter entirely — one
    paginated pass returns every region's meters (the Azure shard, since Azure's
    compact JSON feed isn't disk-bound the way AWS's per-region offers are)."""
    name = fetch["service_name"]
    if regions == ALL:
        filters = [f"serviceName eq '{name}' and priceType eq 'Consumption'"]
    else:
        filters = [
            f"serviceName eq '{name}' and armRegionName eq '{region}' "
            "and priceType eq 'Consumption'"
            for region in regions
        ]
    items: list[dict] = []
    for flt in filters:
        url = "https://prices.azure.com/api/retail/prices?" + urllib.parse.urlencode(
            {"$filter": flt}
        )
        while url:
            d = _get_json(url)
            items.extend(d.get("Items", []))
            url = d.get("NextPageLink")
    return {"Items": items}


def fetch_gcp(fetch: dict, regions: list[str]) -> dict:
    """GCP Cloud Billing Catalog — page through a service's SKUs (needs a key).

    ``regions`` is accepted for adapter-interface symmetry but ignored: the
    catalog isn't region-filtered — each SKU carries its own ``serviceRegions``,
    which the adapter expands to one Unit per (SKU, region), so GCP is already
    multi-region for free (#1025)."""
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
    add_shard_args(ap)
    args = ap.parse_args()

    config = yaml.safe_load(args.config.read_text())
    # The per-provider region set the sheet is generated across (#1025). CI shards
    # override this per job (--region / --all-regions / --global-only); with no
    # flags this curated set is the single-runner / local-dev default.
    region_sets = config.get("regions", {})
    try:
        for cfg in config["recipes"]:
            provider = cfg["provider"]
            if not matches(cfg, args):
                continue
            fetch = cfg.get("fetch")
            if not fetch:
                print(
                    f"  {provider}/{cfg['recipe']}: no fetch block, skipping",
                    file=sys.stderr,
                )
                continue
            regions = regions_for(cfg, args, region_sets)
            out = cfg["offer"]
            out = Path(out) if Path(out).is_absolute() else HERE / out
            out.parent.mkdir(parents=True, exist_ok=True)
            scope = (
                "global"
                if regions is None
                else ("all regions" if regions == ALL else f"{len(regions)} region(s)")
            )
            print(
                f"  fetching {provider}/{cfg['recipe']} → {out} ({scope}) …",
                file=sys.stderr,
            )
            offer = _FETCHERS[provider](fetch, regions)
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
