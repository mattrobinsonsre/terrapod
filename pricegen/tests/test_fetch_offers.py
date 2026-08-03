"""Unit tests for the offer fetcher's stream-filter predicate (#893).

`_keep` is the pure filter applied while streaming a large AWS offer (e.g. the
~450 MB AmazonEC2 offer) so only the needed products land in RAM. Its semantics
mirror the recipe's canonical/select: an attribute filter passes when the attr
is ABSENT (canonical "apply only where present") or its value is allowed.
"""

from __future__ import annotations

import pytest

import pricegen.fetch_offers as fo
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
    assert not fo._keep(_p(family="Storage"), _FAMS, _KEEP)


def test_matching_product_kept():
    assert fo._keep(_p(operatingSystem="Linux", tenancy="Shared"), _FAMS, _KEEP)


def test_disallowed_value_dropped():
    assert not fo._keep(_p(operatingSystem="RHEL", tenancy="Shared"), _FAMS, _KEEP)
    assert not fo._keep(_p(operatingSystem="Linux", tenancy="Dedicated"), _FAMS, _KEEP)


def test_absent_attr_is_kept_mirrors_canonical():
    # marketoption absent on some SKUs -> kept (canonical applies only where present).
    assert fo._keep(_p(operatingSystem="Linux", tenancy="Shared"), _FAMS, _KEEP)


def test_no_keep_attrs_keeps_any_in_family():
    assert fo._keep(_p(family="Storage", volumeApiName="gp3"), {"Storage"}, {})


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
    out = fo.fetch_aws({"service_code": "AmazonS3"}, ["us-east-1", "eu-west-1"])
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
    out = fo.fetch_aws({"service_code": "AmazonS3"}, ["us-east-1", "ap-south-1"])
    assert set(out["products"]) == {"SKU_E1"}


def test_fetch_aws_global_ignores_regions(monkeypatch):
    calls = []

    def fake_get_json(url):
        calls.append(url)
        return {"products": {"G": {}}}

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    out = fo.fetch_aws({"service_code": "AmazonRoute53", "global": True}, ["us-east-1"])
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
    out = fo.fetch_aws(
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
    out = fo.fetch_azure({"service_name": "Storage"}, ["eastus", "westeurope"])
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
    out = fo.fetch_aws({"service_code": "AmazonS3"}, ALL)
    # one product per region key in the index
    assert len(out["products"]) == 3


def test_fetch_azure_all_regions_drops_filter(monkeypatch):
    # ALL -> a single query with no armRegionName filter returns every region.
    captured = {}

    def fake_get_json(url):
        captured["url"] = url
        return {"Items": [{"armRegionName": "eastus"}, {"armRegionName": "westus"}]}

    monkeypatch.setattr(fo, "_get_json", fake_get_json)
    out = fo.fetch_azure({"service_name": "Storage"}, ALL)
    assert "armRegionName" not in captured["url"]  # no region filter
    assert {i["armRegionName"] for i in out["Items"]} == {"eastus", "westus"}


class TestUrlopenRetrying:
    """Bounded retry around the offer downloads (#1251).

    The weekly publish fans out to ~36 AWS regions, and every shard must
    succeed for the pricesheet to be published. Before this, each download was
    a bare `urlopen`, so one transient reset failed a shard and with it the
    whole run — which is exactly what happened on 2026-07-27:
    `ConnectionResetError [Errno 104]` mid-TLS-handshake for ap-southeast-2.

    The offers are large (an EC2 region offer is hundreds of MB) and pulled
    from public endpoints, so a mid-flight cut is not rare — it is the expected
    failure mode at this size and fan-out.
    """

    @staticmethod
    def _patch(monkeypatch, impl):
        calls = {"n": 0}

        def wrapped(req, timeout=None):
            calls["n"] += 1
            return impl(calls["n"])

        monkeypatch.setattr(fo.urllib.request, "urlopen", wrapped)
        monkeypatch.setattr(fo, "_BACKOFF", 0.001)
        return calls

    def test_recovers_from_a_transient_reset(self, monkeypatch):
        sentinel = object()

        def impl(n):
            if n < 3:
                raise fo.urllib.error.URLError(ConnectionResetError(104, "reset"))
            return sentinel

        calls = self._patch(monkeypatch, impl)
        assert fo._urlopen_retrying("https://example.invalid/o.json") is sentinel
        assert calls["n"] == 3

    def test_gives_up_after_a_bounded_number_of_attempts(self, monkeypatch):
        def impl(_n):
            raise fo.urllib.error.URLError(ConnectionResetError(104, "reset"))

        calls = self._patch(monkeypatch, impl)
        with pytest.raises(RuntimeError, match="giving up"):
            fo._urlopen_retrying("https://example.invalid/o.json")
        assert calls["n"] == fo._RETRIES

    def test_a_4xx_is_final_and_not_retried(self, monkeypatch):
        """A 404 is a real answer — a withdrawn offer or a wrong URL. Retrying
        it just delays a failure that will not change."""

        def impl(_n):
            raise fo.urllib.error.HTTPError("u", 404, "Not Found", {}, None)

        calls = self._patch(monkeypatch, impl)
        with pytest.raises(fo.urllib.error.HTTPError):
            fo._urlopen_retrying("https://example.invalid/gone.json")
        assert calls["n"] == 1

    def test_a_5xx_is_retried(self, monkeypatch):
        def impl(_n):
            raise fo.urllib.error.HTTPError("u", 503, "Unavailable", {}, None)

        calls = self._patch(monkeypatch, impl)
        with pytest.raises(RuntimeError):
            fo._urlopen_retrying("https://example.invalid/o.json")
        assert calls["n"] == fo._RETRIES
