"""Unit tests for shard selection (#1025 multi-region).

`matches` + `regions_for` decide which recipes and which regions a CI shard
covers. fetch_offers and publish both use them, so they must agree — these pin
the contract.
"""

from __future__ import annotations

import argparse

from pricegen.shard import ALL, matches, regions_for

_REGION_SETS = {"aws": ["us-east-1", "eu-west-1"], "azure": ["eastus"]}


def _args(**kw):
    ns = argparse.Namespace(
        only=None, region=None, global_only=False, all_regions=False
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _r(provider="aws", **fetch):
    return {"provider": provider, "recipe": "r", "fetch": fetch}


def _global(provider="aws", service_code="AmazonRoute53"):
    # `global` is a Python keyword, so a global-offer fetch can't be built via
    # **kwargs — construct the dict directly.
    return {
        "provider": provider,
        "recipe": "r",
        "fetch": {"service_code": service_code, "global": True},
    }


def test_only_filters_provider():
    assert matches(_r("aws"), _args(only="aws"))
    assert not matches(_r("azure"), _args(only="aws"))


def test_region_shard_excludes_global_recipes():
    # An AWS regional shard covers regional recipes, never the global ones.
    assert matches(
        _r("aws", service_code="AmazonRDS"), _args(only="aws", region="us-east-1")
    )
    assert not matches(_global(), _args(only="aws", region="us-east-1"))


def test_global_only_shard_selects_only_global():
    assert matches(_global(), _args(only="aws", global_only=True))
    assert not matches(
        _r("aws", service_code="AmazonRDS"), _args(only="aws", global_only=True)
    )


def test_regions_for_global_returns_none():
    assert regions_for(_global(), _args(), _REGION_SETS) is None


def test_regions_for_single_region_shard():
    assert regions_for(
        _r("aws", service_code="AmazonRDS"),
        _args(region="us-west-2"),
        _REGION_SETS,
    ) == ["us-west-2"]


def test_regions_for_all_regions_flag():
    assert regions_for(_r("aws"), _args(all_regions=True), _REGION_SETS) == ALL


def test_regions_for_provider_default():
    assert regions_for(_r("aws"), _args(), _REGION_SETS) == ["us-east-1", "eu-west-1"]


def test_regions_for_recipe_override_wins_over_default():
    assert regions_for(_r("aws", region="ap-south-1"), _args(), _REGION_SETS) == [
        "ap-south-1"
    ]
