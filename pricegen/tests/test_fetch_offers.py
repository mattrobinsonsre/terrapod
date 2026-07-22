"""Unit tests for the offer fetcher's stream-filter predicate (#893).

`_keep` is the pure filter applied while streaming a large AWS offer (e.g. the
~450 MB AmazonEC2 offer) so only the needed products land in RAM. Its semantics
mirror the recipe's canonical/select: an attribute filter passes when the attr
is ABSENT (canonical "apply only where present") or its value is allowed.
"""

from __future__ import annotations

from pricegen.fetch_offers import _keep

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
