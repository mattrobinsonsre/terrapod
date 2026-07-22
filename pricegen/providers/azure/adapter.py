"""Azure Retail Prices API adapter (#893).

Normalizes an Azure retail-prices offer (a flat ``Items[]`` array) into ``Unit``s.
Azure is the *string-embedded* case: unlike AWS's structured attribute dict, the
variant we need (OS, Spot/Low-Priority) lives inside free-text fields
(``productName``, ``skuName``), so the recipes lean on the engine's regex
resolution rather than clean attributes.

The offer is fetched from the official public Azure Retail Prices API
(``prices.azure.com/api/retail/prices``, no credentials, paginated via
``NextPageLink``). Each item is already a flat dict — its keys (``armSkuName``,
``productName``, ``skuName``, ``meterName``, ``type``, ``armRegionName``,
``unitPrice``, ``unitOfMeasure`` …) become the Unit's attrs verbatim, and the
Unit's *family* is the ``serviceName`` (e.g. ``Virtual Machines``) that a
recipe's ``product_family`` regex matches.
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


def iter_units(offer: dict, *, term: str = "Consumption"):
    """Yield one Unit per retail-price item.

    ``term`` is accepted for adapter-interface symmetry with AWS but Azure's
    Consumption/Reservation distinction lives in each item's ``type`` field, so
    it's filtered by the recipe's canonical dimension (``type: Consumption``),
    not here — every priced item is yielded and the engine selects.
    """
    # A tiered meter (e.g. blob "Data Stored": 0 / 50 TB / 500 TB) arrives as
    # several Items sharing a meterId, differing only in tierMinimumUnits. The
    # feed gives each tier's START but not its END, so we group by meter and set
    # each tier's end to the next tier's start (the last is open-ended). Without
    # this every tier would end at Inf and overlap, double-counting large usage.
    # Keyed by (meterId, type, reservationTerm) so Consumption tiers never mix
    # with Reservation ones. Single-tier meters (VMs, IPs, AKS) → one item per
    # group → end "Inf", exactly as before.
    from collections import defaultdict

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for item in offer.get("Items", []):
        usd = item.get("unitPrice")
        # A $0 retail price is a feed placeholder (e.g. not-yet-GA meters), not a
        # genuinely free resource — skip it so it can't zero out or drag down a
        # size's price. Mirrors the AWS adapter skipping items with no USD price.
        if usd is None or usd == 0:
            continue
        # A tiered meter's tiers share every descriptor and differ only in
        # tierMinimumUnits. `meterId` is NOT a reliable tier-ladder key: Azure
        # reuses one meterId across regions AND across skuNames (e.g. blob "Data
        # Stored" shares a meterId over Hot LRS / GRS / ZRS). So key on the full
        # meter identity — grouping by meterId would merge unrelated meters and
        # then either fabricate a [0,0] tier or drop rows in the dedup below.
        key = (
            item.get("meterName", ""),
            item.get("skuName", ""),
            item.get("productName", ""),
            item.get("type", ""),
            item.get("reservationTerm", ""),
            item.get("armRegionName", ""),
        )
        groups[key].append(item)

    for group in groups.values():
        # tierMinimumUnits is a float (0.0, 51200.0, …); sort ascending so the
        # end boundary is the next tier's start.
        group.sort(key=lambda i: float(i.get("tierMinimumUnits", 0) or 0))
        # Dedup consecutive same-tier entries: the retail feed sometimes lists a
        # meter twice at the same tierMinimumUnits, which would otherwise make a
        # zero-width [start, start] tier that prices to $0.
        items: list[dict] = []
        for it in group:
            t = float(it.get("tierMinimumUnits", 0) or 0)
            if items and float(items[-1].get("tierMinimumUnits", 0) or 0) == t:
                continue
            items.append(it)
        for idx, item in enumerate(items):
            begin = int(float(item.get("tierMinimumUnits", 0) or 0))
            end = (
                "Inf"
                if idx + 1 >= len(items)
                else str(int(float(items[idx + 1].get("tierMinimumUnits", 0) or 0)))
            )
            price = Price(
                usd=str(item["unitPrice"]),
                begin=str(begin),
                end=end,
                unit=item.get("unitOfMeasure", ""),
            )
            yield Unit(item.get("serviceName", ""), item, [price])
