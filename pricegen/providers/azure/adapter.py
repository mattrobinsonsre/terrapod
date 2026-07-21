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
    for item in offer.get("Items", []):
        usd = item.get("unitPrice")
        # A $0 retail price is a feed placeholder (e.g. not-yet-GA meters), not a
        # genuinely free resource — skip it so it can't zero out or drag down a
        # size's price. Mirrors the AWS adapter skipping items with no USD price.
        if usd is None or usd == 0:
            continue
        # tierMinimumUnits is a float (0.0, 51200.0, …); the consumer parses tier
        # bounds with int(), so normalize to an int-string ("0.0" would crash it).
        begin = str(int(float(item.get("tierMinimumUnits", 0) or 0)))
        price = Price(
            usd=str(usd),
            begin=begin,
            end="Inf",
            unit=item.get("unitOfMeasure", ""),
        )
        yield Unit(item.get("serviceName", ""), item, [price])
