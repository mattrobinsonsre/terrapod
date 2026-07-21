"""Unit tests for the GCP Cloud Billing Catalog adapter (#933)."""

from __future__ import annotations

from pricegen.providers.gcp import adapter


def _sku(
    desc, units="0", nanos=0, regions=("us-central1",), rg="CPU", usage="OnDemand"
):
    return {
        "description": desc,
        "category": {"resourceGroup": rg, "usageType": usage},
        "serviceRegions": list(regions),
        "pricingInfo": [
            {
                "pricingExpression": {
                    "usageUnit": "h",
                    "tieredRates": [{"unitPrice": {"units": units, "nanos": nanos}}],
                }
            }
        ],
    }


def test_rate_units_and_nanos_combined():
    units = list(
        adapter.iter_units(
            {"skus": [_sku("N2 Instance Core", units="0", nanos=31611000)]}
        )
    )
    assert units[0].prices[0].usd == "0.0316110000"
    assert units[0].attrs["description"] == "N2 Instance Core"
    assert units[0].attrs["usage_type"] == "OnDemand"


def test_multi_region_sku_expands_to_one_unit_per_region():
    sku = _sku("N2 Instance Ram", nanos=4237000, regions=("us-central1", "us-east1"))
    units = list(adapter.iter_units({"skus": [sku]}))
    assert {u.attrs["region"] for u in units} == {"us-central1", "us-east1"}
    assert all(u.prices[0].usd == "0.0042370000" for u in units)


def test_sku_without_pricing_skipped():
    bad = {
        "description": "x",
        "category": {},
        "serviceRegions": ["us-central1"],
        "pricingInfo": [],
    }
    assert list(adapter.iter_units({"skus": [bad]})) == []
