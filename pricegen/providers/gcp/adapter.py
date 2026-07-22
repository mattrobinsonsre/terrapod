"""GCP Cloud Billing Catalog adapter (#893).

Normalizes a GCP service's SKU list into ``Unit``s. GCP is the *computed* case:
Compute Engine doesn't price a machine type directly — it prices **per vCPU-core
hour** and **per GiB-RAM hour** separately (e.g. "N2 Instance Core running in
Americas", "N2 Instance Ram running in Americas"), and a machine type's cost is
``vCPU × core-rate + RAM_GiB × ram-rate``. The recipe's ``computed`` block (run
by ``engine.generate_computed``) does that assembly; this adapter just surfaces
each SKU's rate.

The offer is fetched from the public Cloud Billing Catalog API
(``cloudbilling.googleapis.com/v1/services/{id}/skus``) which needs a free
read-only API key. Each SKU can apply to several ``serviceRegions``; we expand
to **one Unit per (SKU, region)** so ``region`` is a scalar attr the recipe can
match, mirroring how AWS/Azure carry a single region per Unit. The Unit family
is the SKU ``description`` (the recipe extracts the machine family + Core/Ram
kind from it by regex).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from pricegen.models import Price, Unit


def load(path: Path) -> dict:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt") as f:
        return json.load(f)


def _rate(sku: dict) -> str | None:
    infos = sku.get("pricingInfo") or []
    if not infos:
        return None
    tiers = infos[0].get("pricingExpression", {}).get("tieredRates") or []
    # Take the first tier with a NON-ZERO unit price. Many GCP SKUs lead with a
    # $0 tier that encodes an always-free allotment (e.g. pd-standard's first
    # 30 GiB, a static IP's first hour) before the real marginal rate kicks in.
    # We bill the marginal rate from unit 0 — i.e. we do NOT apply the
    # account-wide free tier per-resource, exactly as we drop AWS/Azure free
    # tiers. An all-zero SKU returns None (skipped, like the AWS $0-skip). This
    # leaves single-tier SKUs (compute Core/Ram, SSD/Balanced PD) unchanged.
    for t in tiers:
        up = t.get("unitPrice", {})
        val = int(up.get("units", "0") or 0) + up.get("nanos", 0) / 1e9
        if val > 0:
            return f"{val:.10f}"
    return None


def iter_units(offer: dict, *, term: str = "OnDemand"):
    """Yield one Unit per (SKU, serviceRegion).

    ``term`` is accepted for interface symmetry; GCP's OnDemand/Commitment split
    is in each SKU's ``category.usageType``, filtered by the recipe's canonical
    dimension, not here.
    """
    for sku in offer.get("skus", []):
        usd = _rate(sku)
        if usd is None:
            continue
        cat = sku.get("category", {})
        pe = (sku.get("pricingInfo") or [{}])[0].get("pricingExpression", {})
        base = {
            "description": sku.get("description", ""),
            "resource_group": cat.get("resourceGroup", ""),
            "usage_type": cat.get("usageType", ""),
            "usage_unit": pe.get("usageUnit", ""),
        }
        price = Price(usd=usd, begin="0", end="Inf", unit=base["usage_unit"])
        for region in sku.get("serviceRegions", []) or [""]:
            yield Unit(base["description"], {**base, "region": region}, [price])
