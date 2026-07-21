"""Pricesheet products — port of OpenInfraQuote's ``oiq_prices.ml`` (MPL-2.0).

Each row of OpenInfraQuote's ``prices.csv`` is a *product* — a priced cloud
line-item with the match rules that attach it to Terraform resources. Columns::

    service, product_family, match_set, pricing_match_set, price, price_type, ccy

* ``match_set``          — resource match expression (``type=aws_db_instance&values.…``);
                           empty ⇒ the row is skipped.
* ``pricing_match_set``  — billing dimensions (region, purchase_option,
                           start/end_usage_amount, …).
* ``price`` / ``price_type`` — the unit price and how it is charged:
  ``t`` per-time, ``o`` per-operation, ``d`` per-data, ``a=<attr>`` per unit of
  a resource attribute.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import IO

import yaml

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
    attr: str | None = None  # set only when kind is ATTR


def _parse_price_type(price_type: str, value: float) -> Price:
    if price_type == "t":
        return Price(PriceKind.PER_TIME, value)
    if price_type == "o":
        return Price(PriceKind.PER_OPERATION, value)
    if price_type == "d":
        return Price(PriceKind.PER_DATA, value)
    if price_type.startswith("a="):
        return Price(PriceKind.ATTR, value, attr=price_type[2:])
    raise ValueError(f"unknown price type: {price_type!r}")


@dataclass(frozen=True)
class Product:
    service: str
    product_family: str
    match_set: MatchSet
    pricing_match_set: MatchSet
    price: Price
    ccy: str


class EmptyMatchSet(Exception):
    """Row carried an empty resource match set — skipped (not an error)."""


def product_of_row(row: list[str]) -> Product:
    """Parse one CSV row into a :class:`Product`.

    Raises :class:`EmptyMatchSet` for rows with a blank match-set column (these
    are silently skipped, mirroring ``oiq_prices.Product.of_row``), and
    ``ValueError`` for genuinely malformed rows.
    """
    if len(row) != 7:
        raise ValueError(f"expected 7 columns, got {len(row)}: {row!r}")
    service, product_family, match_set, pricing_match_set, price, price_type, ccy = row
    if match_set == "":
        raise EmptyMatchSet
    try:
        price_value = float(price)
    except ValueError as exc:
        raise ValueError(f"invalid price: {price!r}") from exc
    price_info = _parse_price_type(price_type, price_value)
    return Product(
        service=service,
        product_family=product_family,
        match_set=MatchSet.of_string(match_set),
        pricing_match_set=MatchSet.of_string(pricing_match_set),
        price=price_info,
        ccy=ccy,
    )


_YAML_SCHEMA_PREFIX = "terrapod-pricesheet/"


def _product_of_yaml(entry: dict, currency: str) -> Product:
    """Parse one product mapping from a Terrapod YAML pricesheet."""
    match_set = entry.get("match", "")
    if match_set == "":
        raise EmptyMatchSet
    price_value = float(entry["price"])
    return Product(
        service=entry.get("service", ""),
        product_family=entry.get("family", ""),
        match_set=MatchSet.of_string(match_set),
        pricing_match_set=MatchSet.of_string(entry.get("pricing", "")),
        price=_parse_price_type(entry["price_type"], price_value),
        ccy=currency,
    )


def load_pricesheet(fp: IO[str]) -> Iterator[Product]:
    """Stream products from an open pricesheet file object.

    Two formats are accepted, auto-detected from the first line:

    * **OpenInfraQuote CSV** (``service,product_family,…`` header) — streamed row
      by row so a ~200k-row sheet is never fully materialised.
    * **Terrapod YAML** (``schema: terrapod-pricesheet/vN``, produced by
      ``pricegen``) — a self-describing ``{schema, currency, products: [...]}``
      document. Our sheet is far smaller and more targeted, so it's parsed whole.

    Rows/products with an empty resource match set are dropped (mirrors
    ``oiq_prices.Product.of_row``).
    """
    first = fp.readline()
    if first.lstrip().startswith("schema:") and _YAML_SCHEMA_PREFIX in first:
        doc = yaml.safe_load(first + fp.read()) or {}
        currency = doc.get("currency", "USD")
        for entry in doc.get("products", []):
            try:
                yield _product_of_yaml(entry, currency)
            except EmptyMatchSet:
                continue
        return
    # CSV: ``first`` was the header line; the remaining lines are data rows.
    for row in csv.reader(fp):
        if not row:
            continue
        try:
            yield product_of_row(row)
        except EmptyMatchSet:
            continue
