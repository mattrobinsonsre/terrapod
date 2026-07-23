"""Unit tests for the offer fetcher's stream-filter predicate (#893).

`_keep` is the pure filter applied while streaming a large AWS offer (e.g. the
~450 MB AmazonEC2 offer) so only the needed products land in RAM. Its semantics
mirror the recipe's canonical/select: an attribute filter passes when the attr
is ABSENT (canonical "apply only where present") or its value is allowed.
"""

from __future__ import annotations

import pricegen.fetch_offers as fo
from pricegen.fetch_offers import _keep, fetch_aws, fetch_azure
from pricegen.shard import ALL

_FAMS = {"Compute Instance"}
_KEEP = {
    "operatingSystem": {"Linux", "Windows"},
    "tenancy": {"Shared"},
    "marketoption": {"OnDemand"},
}


def _p(family="Compute Instance", **attrs):
    return {"productFamily": family, "attributes": attrs}


def test_wrong_family_dropped():
    assert not _keep(_p(family="Storage"), _FAMS, _KEEP)


def test_matching_product_kept():
    assert _keep(_p(operatingSystem="Linux", tenancy="Shared"), _FAMS, _KEEP)


def test_disallowed_value_dropped():
    assert not _keep(_p(operatingSystem="RHEL", tenancy="Shared"), _FAMS, _KEEP)
    assert not _keep(_p(operatingSystem="Linux", tenancy="Dedicated"), _FAMS, _KEEP)


def test_absent_attr_is_kept_mirrors_canonical():
    # marketoption absent on some SKUs -> kept (canonical applies only where present).
    assert _keep(_p(operatingSystem="Linux", tenancy="Shared"), _FAMS, _KEEP)


def test_no_keep_attrs_keeps_any_in_family():
    assert _keep(_p(family="Storage", volumeApiName="gp3"), {"Storage"}, {})


# --- multi-region fetch + merge (#1025) ------------------------------------


def test_fetch_aws_merges_regions(monkeypatch):
    # region_index lists two regions; each region's small offer contributes its
    # own products + OnDemand terms, merged into one offer dict.
    index = {
        "regions": {
            "us-east-1": {"currentVersionUrl": "/e1.json"},
            "eu-west-1": {"currentVersionUrl": "/w1.json"},
        }
    }
    offers = {
        "/e1.json": {
            "products": {"SKU_E1": {"attributes": {"regionCode": "us-east-1"}}},
            "terms": {"OnDemand": {"SKU_E1": {"t": 1}}},
        },
        "/w1.json": {
            "products": {"SKU_W1": {"attributes": {"regionCode": "eu-west-1"}}},
            "terms": {"OnDemand": {"SKU_W1": {"t": 2}}},
        },
    }

    def fake_get_json(url):
        if url.endswith("region_index.json"):
            return index
        return offers[url[len(fo._AWS_BULK) :]]

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    out = fetch_aws({"service_code": "AmazonS3"}, ["us-east-1", "eu-west-1"])
    assert set(out["products"]) == {"SKU_E1", "SKU_W1"}
    assert set(out["terms"]["OnDemand"]) == {"SKU_E1", "SKU_W1"}


def test_fetch_aws_skips_region_absent_from_index(monkeypatch):
    index = {"regions": {"us-east-1": {"currentVersionUrl": "/e1.json"}}}

    def fake_get_json(url):
        if url.endswith("region_index.json"):
            return index
        return {"products": {"SKU_E1": {}}, "terms": {"OnDemand": {}}}

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    # ap-south-1 isn't in the index — skipped, not an error.
    out = fetch_aws({"service_code": "AmazonS3"}, ["us-east-1", "ap-south-1"])
    assert set(out["products"]) == {"SKU_E1"}


def test_fetch_aws_global_ignores_regions(monkeypatch):
    calls = []

    def fake_get_json(url):
        calls.append(url)
        return {"products": {"G": {}}}

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    out = fetch_aws({"service_code": "AmazonRoute53", "global": True}, ["us-east-1"])
    # one fetch of current/index.json, no region_index involved
    assert len(calls) == 1 and calls[0].endswith("current/index.json")
    assert out["products"] == {"G": {}}


def test_fetch_aws_large_offer_merges_via_stream_filter(monkeypatch):
    index = {
        "regions": {
            "us-east-1": {"currentVersionUrl": "/e1.json"},
            "eu-west-1": {"currentVersionUrl": "/w1.json"},
        }
    }
    monkeypatch.setattr(
        fo, "_get_json", lambda url: index if "region_index" in url else {}
    )
    seen_urls = []

    def fake_stream(region_url, families, keep):
        seen_urls.append(region_url)
        sku = "E1" if region_url.endswith("/e1.json") else "W1"
        return {"products": {sku: {}}, "terms": {"OnDemand": {sku: {}}}}

    monkeypatch.setattr(fo, "_stream_filter_aws", fake_stream)
    out = fetch_aws(
        {"service_code": "AmazonEC2", "families": ["Compute Instance"]},
        ["us-east-1", "eu-west-1"],
    )
    assert set(out["products"]) == {"E1", "W1"}
    assert len(seen_urls) == 2  # stream-filtered once per region


def test_fetch_azure_concats_regions(monkeypatch):
    pages = {
        "eastus": {"Items": [{"armRegionName": "eastus"}]},
        "westeurope": {"Items": [{"armRegionName": "westeurope"}]},
    }

    def fake_get_json(url):
        region = "eastus" if "eastus" in url else "westeurope"
        return pages[region]  # single page each (no NextPageLink)

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    out = fetch_azure({"service_name": "Storage"}, ["eastus", "westeurope"])
    assert {i["armRegionName"] for i in out["Items"]} == {"eastus", "westeurope"}


def test_fetch_aws_all_regions_discovers_from_index(monkeypatch):
    # ALL -> use every region in the service's region_index (the AWS coverage).
    index = {
        "regions": {
            "us-east-1": {"currentVersionUrl": "/e1.json"},
            "eu-west-1": {"currentVersionUrl": "/w1.json"},
            "ap-northeast-1": {"currentVersionUrl": "/n1.json"},
        }
    }

    def fake_get_json(url):
        if url.endswith("region_index.json"):
            return index
        sku = url.rsplit("/", 1)[1]
        return {"products": {sku: {}}, "terms": {"OnDemand": {sku: {}}}}

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    out = fetch_aws({"service_code": "AmazonS3"}, ALL)
    # one product per region key in the index
    assert len(out["products"]) == 3


def test_fetch_azure_all_regions_drops_filter(monkeypatch):
    # ALL -> a single query with no armRegionName filter returns every region.
    captured = {}

    def fake_get_json(url):
        captured["url"] = url
        return {"Items": [{"armRegionName": "eastus"}, {"armRegionName": "westus"}]}

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    out = fetch_azure({"service_name": "Storage"}, ALL)
    assert "armRegionName" not in captured["url"]  # no region filter
    assert {i["armRegionName"] for i in out["Items"]} == {"eastus", "westus"}
