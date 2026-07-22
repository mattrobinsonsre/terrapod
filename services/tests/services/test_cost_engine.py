"""Tests for the native cost-estimation engine (issue #871).

The engine is a faithful Python port of OpenInfraQuote's matcher + pricer. These
tests pin the ported behaviour with hand-verified fixtures — a tiny pricesheet +
plan/state whose totals can be computed by hand — plus focused unit coverage of
each primitive (match sets, the match-query language, CSV product parsing, the
plan/state adapters, and the usage catalogue). Bit-exact agreement with the real
``oiq`` binary is pinned separately by the differential oracle in
``test_cost_differential.py``.
"""

from __future__ import annotations

import io

import pytest

from terrapod.services.cost import estimate
from terrapod.services.cost.match_query import MatchQuery
from terrapod.services.cost.match_set import MatchSet
from terrapod.services.cost.prices import EmptyMatchSet, PriceKind, product_of_row
from terrapod.services.cost.range import Range, overlap
from terrapod.services.cost.tf import (
    Resource,
    flatten,
    provider_regions,
    resolve_region,
    resources_from_json,
)
from terrapod.services.cost.usage import DATA, bound_to_usage_amount, default_entries, match_entry

# ---------------------------------------------------------------------------
# match_set
# ---------------------------------------------------------------------------


def test_match_set_of_string_parses_and_pct_decodes():
    ms = MatchSet.of_string("type=aws_instance&values.name=my%20box&empty=")
    assert ms.contains("type", "aws_instance")
    assert ms.contains("values.name", "my box")  # percent-decoded
    assert ms.contains("empty", "")
    assert ms.find_by_key("missing") is None


def test_match_set_drops_empty_segments():
    assert MatchSet.of_string("&&a=b&&") == MatchSet.of_list([("a", "b")])


def test_match_set_missing_equals_raises():
    with pytest.raises(ValueError):
        MatchSet.of_string("novalue")


def test_match_set_subset_and_union():
    resource = MatchSet.of_list(
        [("type", "aws_instance"), ("values.size", "large"), ("mode", "managed")]
    )
    product = MatchSet.of_list([("type", "aws_instance")])
    assert product.is_subset_of(resource)
    assert not resource.is_subset_of(product)
    u = product.union(MatchSet.of_list([("region", "us-east-1")]))
    assert u.contains("type", "aws_instance") and u.contains("region", "us-east-1")


# ---------------------------------------------------------------------------
# match_query
# ---------------------------------------------------------------------------


def _ms(*pairs: tuple[str, str]) -> MatchSet:
    return MatchSet.of_list(list(pairs))


def test_match_query_equals_and_key():
    ms = _ms(("type", "aws_instance"), ("os", "linux"))
    assert MatchQuery.of_string("type = aws_instance").eval(ms)
    assert not MatchQuery.of_string("type = aws_lambda_function").eval(ms)
    assert MatchQuery.of_string("os").eval(ms)  # key existence
    assert not MatchQuery.of_string("region").eval(ms)


def test_match_query_boolean_precedence_and_parens():
    ms = _ms(("a", "1"), ("b", "2"))
    # OR < AND < NOT: `a = 1 and b = 3 or a = 1` -> (a=1 and b=3) or a=1 -> True
    assert MatchQuery.of_string("a = 1 and b = 3 or a = 1").eval(ms)
    assert not MatchQuery.of_string("a = 1 and b = 3").eval(ms)
    assert MatchQuery.of_string("not b = 3").eval(ms)
    assert MatchQuery.of_string("(a = 1 or a = 9) and b = 2").eval(ms)
    assert not MatchQuery.of_string("not (a = 1)").eval(ms)


def test_match_query_empty_matches_everything():
    assert MatchQuery.of_string("").eval(_ms())
    assert MatchQuery.of_string("   ").eval(_ms(("x", "y")))


def test_match_query_real_usage_entry_with_not_and_parens():
    # From the vendored usage.json (Lambda x86 arch entry).
    q = MatchQuery.of_string(
        "type = aws_lambda_function and service_class = duration "
        "and (not values.architectures or values.architectures=x86) and arch=x86"
    )
    ms = _ms(
        ("type", "aws_lambda_function"),
        ("service_class", "duration"),
        ("arch", "x86"),
    )  # no values.architectures -> `not values.architectures` is True
    assert q.eval(ms)
    ms_arm = _ms(
        ("type", "aws_lambda_function"),
        ("service_class", "duration"),
        ("arch", "x86"),
        ("values.architectures", "arm64"),
    )
    assert not q.eval(ms_arm)


# ---------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------


def test_product_of_row_price_types():
    base = ["AmazonEC2", "Compute", "type=aws_instance", "region=us-east-1", "0.10", "t", "USD"]
    assert product_of_row(base).price.kind is PriceKind.PER_TIME
    assert product_of_row([*base[:5], "o", "USD"]).price.kind is PriceKind.PER_OPERATION
    assert product_of_row([*base[:5], "d", "USD"]).price.kind is PriceKind.PER_DATA
    attr = product_of_row([*base[:5], "a=values.storage", "USD"])
    assert attr.price.kind is PriceKind.ATTR and attr.price.attr == "values.storage"


def test_product_of_row_empty_match_set_skipped():
    with pytest.raises(EmptyMatchSet):
        product_of_row(["S", "F", "", "region=x", "0.1", "t", "USD"])


def test_load_pricesheet_reads_terrapod_yaml():
    # the format pricegen publishes (#893) — auto-detected via the schema line.
    import io

    from terrapod.services.cost.prices import PriceKind, load_pricesheet

    sheet = (
        "schema: terrapod-pricesheet/v1\n"
        "currency: USD\n"
        "products:\n"
        "- service: AmazonEC2\n"
        "  family: Compute Instance\n"
        "  match: type=aws_instance&values.instance_type=m5.large\n"
        "  pricing: region=us-east-1&service_class=instance\n"
        "  price: '0.096'\n"
        "  price_type: t\n"
        "- service: S\n"  # empty match -> skipped
        "  family: F\n"
        "  match: ''\n"
        "  pricing: region=us-east-1\n"
        "  price: '1'\n"
        "  price_type: t\n"
    )
    products = list(load_pricesheet(io.StringIO(sheet)))
    assert len(products) == 1  # the empty-match product is dropped
    p = products[0]
    assert p.service == "AmazonEC2" and p.ccy == "USD"
    assert p.price.kind is PriceKind.PER_TIME and p.price.value == 0.096
    assert p.match_set.contains("values.instance_type", "m5.large")


def test_load_pricesheet_still_reads_csv():
    import io

    from terrapod.services.cost.prices import load_pricesheet

    sheet = (
        "service,product_family,match_set,pricing_match_set,price,price_type,ccy\n"
        "AmazonEC2,Compute,type=aws_instance&values.x=1,region=us-east-1,0.10,t,USD\n"
    )
    products = list(load_pricesheet(io.StringIO(sheet)))
    assert len(products) == 1 and products[0].service == "AmazonEC2"


# ---------------------------------------------------------------------------
# tf adapter
# ---------------------------------------------------------------------------


def test_flatten_scalars_and_lists():
    flat = dict(
        flatten(
            {
                "type": "aws_instance",
                "values": {"count": 3, "enabled": True, "note": None, "tags": ["a", "b"]},
            }
        )
    )
    assert flat["type"] == "aws_instance"
    assert flat["values.count"] == "3"
    assert flat["values.enabled"] == "true"  # bool lowercased
    assert flat["values.note"] == "null"
    # list reuses prefix; set collapses -> last wins in dict() but both present in pairs
    tags = [v for k, v in flatten({"tags": ["a", "b"]}) if k == "tags"]
    assert set(tags) == {"a", "b"}


def _plan(resources, prior=None):
    plan = {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "planned_values": {"root_module": {"resources": resources}},
    }
    if prior is not None:
        plan["prior_state"] = {"values": {"root_module": {"resources": prior}}}
    return plan


def _res(addr, rtype, name, values):
    return {"address": addr, "type": rtype, "name": name, "mode": "managed", "values": values}


def test_resources_from_plan_add_remove_noop():
    web = _res("aws_instance.web", "aws_instance", "web", {"instance_type": "m5.large"})
    db = _res("aws_db_instance.db", "aws_db_instance", "db", {"instance_class": "db.t3.medium"})
    old = _res("aws_instance.old", "aws_instance", "old", {"instance_type": "t2.micro"})
    plan = _plan(resources=[web, db], prior=[web, old])
    got = {r.address: change for r, change in resources_from_plan_helper(plan)}
    assert got == {
        "aws_instance.web": "noop",
        "aws_db_instance.db": "add",
        "aws_instance.old": "remove",
    }


def resources_from_plan_helper(plan):
    from terrapod.services.cost.tf import resources_from_plan

    return resources_from_plan(plan)


def test_resources_from_state_v4_lifts_attributes():
    state = {
        "version": 4,
        "terraform_version": "1.9.0",
        "resources": [
            {
                "mode": "managed",
                "type": "aws_instance",
                "name": "web",
                "instances": [{"attributes": {"instance_type": "m5.large"}}],
            },
            {
                "mode": "data",
                "type": "aws_ami",
                "name": "ubuntu",
                "instances": [{"attributes": {}}],
            },
        ],
    }
    resources = resources_from_json(state)
    assert len(resources) == 1  # data source skipped
    res, change = resources[0]
    assert change == "noop"
    assert res.to_match_set().contains("type", "aws_instance")
    assert res.to_match_set().contains("values.instance_type", "m5.large")


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_default_usage_catalogue_loads():
    entries = default_entries()
    # 18 vendored from OpenInfraQuote + Terrapod additions: RDS provisioned IOPS
    # io1/io2 (#928) + Azure Linux VM hours (#931) + GCP Compute hours (#933) +
    # NAT gateway hours+data (#966) + Elastic IP hours (#973) + load balancer
    # hours+LCU (#977) + classic ELB hours+data (#979) + EBS snapshot storage
    # (#981).
    assert len(entries) == 29
    ec2 = match_entry(
        _ms(
            ("type", "aws_instance"),
            ("service_class", "instance"),
            ("purchase_option", "on_demand"),
            ("os", "linux"),
        ),
        entries,
    )
    assert ec2 is not None and ec2.usage is not None
    assert ec2.usage.time == Range(730, 730)


def test_bound_to_usage_amount_clamps_to_tier():
    from terrapod.services.cost.usage import Entry, Usage

    entry = Entry(
        description="d",
        divisor=None,
        match_query=MatchQuery.of_string(""),
        usage=Usage.from_data_range(Range(50, 900)),
    )
    # usage 50..900 clamped into tier [0, 100]: min stays 50, max caps at the
    # tier width 100 -> the slice of usage that falls in this pricing tier.
    bounded = bound_to_usage_amount(DATA, Range(0, 100), entry)
    assert bounded is not None and bounded.usage.data == Range(50, 100)
    # a point usage of 900 into tier [0, 100]: exactly 100 units fall in-tier.
    point = Entry(
        description="d",
        divisor=None,
        match_query=MatchQuery.of_string(""),
        usage=Usage.from_data_range(Range(900, 900)),
    )
    got = bound_to_usage_amount(DATA, Range(0, 100), point)
    assert got is not None and got.usage.data == Range(100, 100)
    # tier entirely above usage -> None
    assert bound_to_usage_amount(DATA, Range(2000, 3000), entry) is None


def test_range_overlap():
    assert overlap(Range(0, 5), Range(3, 10)) == Range(3, 5)
    assert overlap(Range(0, 5), Range(6, 10)) is None


# ---------------------------------------------------------------------------
# end-to-end (hand-verified totals)
# ---------------------------------------------------------------------------

# One on-demand Linux EC2 instance. The EC2-hours usage default is time=730,
# the product is $0.10/hour Per_time, divisor 1 -> 730 * 0.10 = $73.00/month.
_EC2_ROW = (
    "AmazonEC2,Compute Instance,type=aws_instance,"
    "service_class=instance&purchase_option=on_demand&os=linux&region=us-east-1,"
    "0.1000000000,t,USD\n"
)
_PRICESHEET = "service,product_family,match_set,pricing_match_set,price,price_type,ccy\n" + _EC2_ROW


def _sheet() -> io.StringIO:
    return io.StringIO(_PRICESHEET)


def test_estimate_plan_add_hand_verified():
    plan = _plan(
        resources=[_res("aws_instance.web", "aws_instance", "web", {"instance_type": "m5.large"})]
    )
    est = estimate(plan, _sheet())
    assert est.currency == "USD"
    assert est.total_min == pytest.approx(73.0)
    assert est.total_max == pytest.approx(73.0)
    assert est.diff_min == pytest.approx(73.0)  # it's an add
    assert est.prev_min == pytest.approx(0.0)
    assert len(est.resources) == 1
    assert est.resources[0].monthly_min == pytest.approx(73.0)
    assert est.resources[0].change == "add"
    assert est.unpriced == []


def test_estimate_state_noop_current_cost():
    state = {
        "format_version": "1.0",
        "terraform_version": "1.9.0",
        "values": {
            "root_module": {"resources": [_res("aws_instance.web", "aws_instance", "web", {})]}
        },
    }
    est = estimate(state, _sheet())
    assert est.total_min == pytest.approx(73.0)
    assert est.diff_min == pytest.approx(0.0)  # noop -> no delta
    assert est.prev_min == pytest.approx(73.0)


def test_estimate_remove_negates_and_diffs():
    web = _res("aws_instance.web", "aws_instance", "web", {})
    plan = _plan(resources=[], prior=[web])
    est = estimate(plan, _sheet())
    assert est.total_min == pytest.approx(-73.0)
    assert est.diff_min == pytest.approx(-73.0)
    assert est.prev_min == pytest.approx(0.0)  # total - diff
    assert est.resources[0].change == "remove"


def test_estimate_unpriced_bucket():
    plan = _plan(resources=[_res("aws_thing.x", "aws_unpriceable", "x", {})])
    est = estimate(plan, _sheet())
    assert est.resources == []
    assert est.total_min == pytest.approx(0.0)
    assert len(est.unpriced) == 1
    assert est.unpriced[0].type == "aws_unpriceable"


def test_estimate_to_dict_shape():
    plan = _plan(resources=[_res("aws_instance.web", "aws_instance", "web", {})])
    d = estimate(plan, _sheet()).to_dict()
    assert set(d) == {"currency", "total", "previous", "diff", "resources", "unpriced"}
    assert d["total"]["min"] == pytest.approx(73.0)
    assert d["resources"][0]["monthly"]["max"] == pytest.approx(73.0)


# ---------------------------------------------------------------------------
# per-resource region resolution (#871)
# ---------------------------------------------------------------------------


def _resource(addr, rtype, values, provider_name=None):
    data = {"address": addr, "type": rtype, "name": addr.split(".")[-1], "values": values}
    if provider_name:
        data["provider_name"] = provider_name
    return Resource(address=addr, name=data["name"], type=rtype, data=data)


def test_resolve_region_from_resource_attribute():
    # AWS v6 `region` on the resource.
    r = _resource("aws_instance.web", "aws_instance", {"region": "eu-west-1"})
    assert resolve_region(r, {}, "us-east-1") == "eu-west-1"
    # Azure `location`.
    r2 = _resource("azurerm_vm.a", "azurerm_virtual_machine", {"location": "westeurope"})
    assert resolve_region(r2, {}, "us-east-1") == "westeurope"


def test_resolve_region_from_provider_config():
    r = _resource(
        "aws_instance.web",
        "aws_instance",
        {"instance_type": "m5.large"},  # no region attr (pre-v6)
        provider_name="registry.terraform.io/hashicorp/aws",
    )
    pmap = {"registry.terraform.io/hashicorp/aws": "ap-south-1"}
    assert resolve_region(r, pmap, "us-east-1") == "ap-south-1"


def test_resolve_region_falls_back_to_default():
    r = _resource("aws_instance.web", "aws_instance", {"instance_type": "m5.large"})
    assert resolve_region(r, {}, "us-east-1") == "us-east-1"


def test_resolve_region_derives_from_gcp_zone():
    # a google_compute_instance carries `zone`, not `region` (#933).
    r = _resource(
        "google_compute_instance.a",
        "google_compute_instance",
        {"machine_type": "n2-standard-4", "zone": "us-central1-a"},
    )
    assert resolve_region(r, {}, "us-east-1") == "us-central1"
    # an explicit region attribute still wins over zone.
    r2 = _resource(
        "google_compute_instance.b",
        "google_compute_instance",
        {"region": "europe-west1", "zone": "us-central1-a"},
    )
    assert resolve_region(r2, {}, "us-east-1") == "europe-west1"


def test_provider_regions_extracts_constant_only():
    plan = {
        "configuration": {
            "provider_config": {
                "aws": {
                    "full_name": "registry.terraform.io/hashicorp/aws",
                    "expressions": {"region": {"constant_value": "us-west-2"}},
                },
                "aws.var": {
                    "full_name": "registry.terraform.io/hashicorp/aws",
                    "expressions": {"region": {"references": ["var.region"]}},  # not constant
                },
            }
        }
    }
    pmap = provider_regions(plan)
    assert pmap == {"registry.terraform.io/hashicorp/aws": "us-west-2"}


# Two identical EC2 products differing only by region+price. A per-plan region
# would mis-price the other resource; per-resource region prices each correctly.
_MULTI_REGION_SHEET = (
    "service,product_family,match_set,pricing_match_set,price,price_type,ccy\n"
    "AmazonEC2,Compute,type=aws_instance,"
    "service_class=instance&purchase_option=on_demand&os=linux&region=us-east-1,0.10,t,USD\n"
    "AmazonEC2,Compute,type=aws_instance,"
    "service_class=instance&purchase_option=on_demand&os=linux&region=eu-west-1,0.20,t,USD\n"
)


def test_estimate_prices_each_resource_in_its_own_region():
    plan = _plan(
        resources=[
            _res("aws_instance.us", "aws_instance", "us", {"region": "us-east-1"}),
            _res("aws_instance.eu", "aws_instance", "eu", {"region": "eu-west-1"}),
        ]
    )
    est = estimate(plan, io.StringIO(_MULTI_REGION_SHEET))
    by_addr = {r.address: r for r in est.resources}
    # us-east-1 @ $0.10/hr * 730 = $73; eu-west-1 @ $0.20/hr * 730 = $146.
    assert by_addr["aws_instance.us"].monthly_min == pytest.approx(73.0)
    assert by_addr["aws_instance.us"].monthly_max == pytest.approx(73.0)
    assert by_addr["aws_instance.eu"].monthly_min == pytest.approx(146.0)
    assert by_addr["aws_instance.eu"].monthly_max == pytest.approx(146.0)
    assert est.total_min == pytest.approx(219.0)
