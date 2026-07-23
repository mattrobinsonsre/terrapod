"""Pricesheet products — one priced cloud line-item apiece.

A *product* is a single priced thing (an on-demand m5.large in us-east-1, a GB of
gp3 storage, a provisioned IOPS) plus the rules that attach it to Terraform
resources. It carries:

* a **resource match set** — which resources it can price
  (``type=aws_db_instance&values.…``); an empty one means the row is skipped;
* a **pricing match set** — its billing dimensions (region, purchase option,
  usage/provision bounds, …);
* a **price** and a **price kind** — the unit rate and how it is charged: per
  unit time (``t``), per operation (``o``), per unit of data (``d``), or per unit
  of a named resource attribute (``a=<attr>``, e.g. ``a=values.size``).

Products come from Terrapod's own self-describing YAML pricesheet (produced by
``pricegen``): a ``{schema, currency, products: [...]}`` document, one mapping
per product.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import IO

from terrapod.services.cost.match_set import MatchSet


class PriceKind(Enum):
    PER_TIME = "t"
    PER_OPERATION = "o"
    PER_DATA = "d"
    ATTR = "a"


@dataclass(frozen=True)
class Price:
    kind: PriceKind
    value: float
    attr: str | None = None  # the attribute name, set only when kind is ATTR


def _parse_price_type(price_type: str, value: float) -> Price:
    """Decode a ``price_type`` token into a :class:`Price`."""
    if price_type.startswith("a="):
        return Price(PriceKind.ATTR, value, attr=price_type[2:])
    try:
        return Price(PriceKind(price_type), value)
    except ValueError:
        raise ValueError(f"unknown price type: {price_type!r}") from None


@dataclass(frozen=True)
class Product:
    service: str
    product_family: str
    match_set: MatchSet
    pricing_match_set: MatchSet
    price: Price
    ccy: str


class EmptyMatchSet(Exception):
    """A product carried a blank resource match set — skipped, not an error."""


def product_from_yaml(entry: dict, currency: str) -> Product:
    """Build a :class:`Product` from one product mapping of a YAML pricesheet.

    Raises :class:`EmptyMatchSet` for a product whose ``match`` is blank (these
    are skipped by the callers), ``ValueError`` for a malformed price/kind.
    """
    match = entry.get("match", "")
    if not match:
        raise EmptyMatchSet
    return Product(
        service=entry.get("service", ""),
        product_family=entry.get("family", ""),
        match_set=MatchSet.parse(match),
        pricing_match_set=MatchSet.parse(entry.get("pricing", "")),
        price=_parse_price_type(entry["price_type"], float(entry["price"])),
        ccy=currency,
    )


def load_pricesheet(fp: IO[str]) -> Iterator[Product]:
    """Yield every product from an open Terrapod YAML pricesheet.

    The document is ``{schema, currency, products: [...]}``; products with a
    blank resource match set are dropped. This whole-document reader is used for
    small sheets and tests — the production path streams the sheet into a SQLite
    index instead (see :mod:`terrapod.services.cost.pricesheet_db`).
    """
    import yaml

    doc = yaml.safe_load(fp.read()) or {}
    currency = doc.get("currency", "USD")
    for entry in doc.get("products", []):
        try:
            yield product_from_yaml(entry, currency)
        except EmptyMatchSet:
            continue
