"""Unit tests for the Azure Retail Prices adapter (#893).

Deterministic — a tiny in-memory ``Items[]`` offer, no network. Covers the two
normalizations the adapter does that the AWS one didn't need: skipping $0
placeholder meters, and coercing Azure's float ``tierMinimumUnits`` to an
int-string (the consumer parses tier bounds with ``int()``, so "0.0" would
crash it).
"""

from __future__ import annotations

from pricegen.providers.azure import adapter


def _offer(items):
    return {"Items": items}


def _item(**kw):
    base = {
        "serviceName": "Virtual Machines",
        "armSkuName": "Standard_D2s_v5",
        "productName": "Virtual Machines Dsv5 Series",
        "skuName": "Standard_D2s_v5",
        "type": "Consumption",
        "armRegionName": "eastus",
        "unitPrice": 0.096,
        "unitOfMeasure": "1 Hour",
        "tierMinimumUnits": 0.0,
    }
    base.update(kw)
    return base


def test_family_is_service_name_and_attrs_are_the_item():
    units = list(adapter.iter_units(_offer([_item()])))
    assert len(units) == 1
    u = units[0]
    assert u.family == "Virtual Machines"
    assert u.attrs["armSkuName"] == "Standard_D2s_v5"
    assert u.prices[0].usd == "0.096"
    assert u.prices[0].unit == "1 Hour"


def test_zero_price_meters_are_skipped():
    units = list(
        adapter.iter_units(_offer([_item(unitPrice=0.0), _item(unitPrice=0.096)]))
    )
    assert len(units) == 1  # the $0 placeholder dropped
    assert units[0].prices[0].usd == "0.096"


def test_none_price_skipped():
    units = list(adapter.iter_units(_offer([_item(unitPrice=None)])))
    assert units == []


def test_tier_minimum_units_float_coerced_to_int_string():
    # 0.0 -> "0"; a tiered boundary like 51200.0 -> "51200" (consumer int()-parses).
    u0 = next(adapter.iter_units(_offer([_item(tierMinimumUnits=0.0)])))
    assert u0.prices[0].begin == "0"
    u1 = next(adapter.iter_units(_offer([_item(tierMinimumUnits=51200.0)])))
    assert u1.prices[0].begin == "51200"


def test_missing_tier_minimum_units_defaults_to_zero():
    item = _item()
    del item["tierMinimumUnits"]
    u = next(adapter.iter_units(_offer([item])))
    assert u.prices[0].begin == "0"
