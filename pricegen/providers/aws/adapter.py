"""AWS Price List Bulk API adapter (#893).

Normalizes an AWS offer file (``products`` + ``terms``) into ``Unit``s. AWS is
the clean case: every product carries a rich structured attribute dict, and its
term's ``priceDimensions`` give the (price, tier-begin, tier-end, unit) tuples.

The offer is fetched from the official public AWS Price List Bulk API (no
credentials). Prototype loads the whole file with ``json.load`` (~2.5 GB RAM for
one EC2 region); the production adapter streams it (ijson).
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


def iter_units(offer: dict, *, term: str = "OnDemand"):
    """Yield one Unit per priced product SKU under the given AWS term."""
    products = offer["products"]
    term_map = offer.get("terms", {}).get(term, {})
    for sku, product in products.items():
        offer_terms = term_map.get(sku)
        if not offer_terms:
            continue
        prices: list[Price] = []
        for t in offer_terms.values():
            for pd in t.get("priceDimensions", {}).values():
                usd = pd.get("pricePerUnit", {}).get("USD")
                # Skip $0 dimensions — they're AWS Free Tier / promotional SKUs
                # (e.g. Lambda's free duration tier, often carrying an empty
                # region), not a real charge. Mirrors the Azure adapter.
                if usd is None or float(usd) == 0:
                    continue
                prices.append(
                    Price(
                        usd,
                        pd.get("beginRange", "0"),
                        pd.get("endRange", "Inf"),
                        pd.get("unit", ""),
                    )
                )
        if not prices:
            continue
        yield Unit(
            product.get("productFamily", ""), product.get("attributes", {}), prices
        )
