"""Tests for the native cost-estimation engine (issue #871).

Pin the engine's behaviour with hand-verified fixtures — a tiny pricesheet plus a
plan/state whose totals can be computed by hand — alongside focused unit coverage
of each primitive: match sets, the match-query language, YAML product parsing, the
plan/state adapters, and the usage catalogue.
"""

from __future__ import annotations

import io

import pytest

from terrapod.services.cost import estimate
from terrapod.services.cost.match_query import MatchQuery
from terrapod.services.cost.match_set import MatchSet
from terrapod.services.cost.prices import EmptyMatchSet, PriceKind, product_from_yaml
from terrapod.services.cost.range import Range, intersect
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


def test_match_set_parse_and_pct_decodes():
    ms = MatchSet.parse("type=aws_instance&values.name=my%20box&empty=")
    assert ms.contains("type", "aws_instance")
    assert ms.contains("values.name", "my box")  # percent-decoded
    assert ms.contains("empty", "")
    assert ms.find_by_key("missing") is None


def test_match_set_drops_empty_segments():
    assert MatchSet.parse("&&a=b&&") == MatchSet.from_pairs([("a", "b")])


def test_match_set_missing_equals_raises():
    with pytest.raises(ValueError):
        MatchSet.parse("novalue")


def test_match_set_subset_and_union():
    resource = MatchSet.from_pairs(
        [("type", "aws_instance"), ("values.size", "large"), ("mode", "managed")]
    )
    product = MatchSet.from_pairs([("type", "aws_instance")])
    assert product.is_subset_of(resource)
    assert not resource.is_subset_of(product)
    u = product.union(MatchSet.from_pairs([("region", "us-east-1")]))
    assert u.contains("type", "aws_instance") and u.contains("region", "us-east-1")


# ---------------------------------------------------------------------------
# match_query
# ---------------------------------------------------------------------------


def _ms(*pairs: tuple[str, str]) -> MatchSet:
    return MatchSet.from_pairs(list(pairs))


def test_match_query_equals_and_key():
    ms = _ms(("type", "aws_instance"), ("os", "linux"))
    assert MatchQuery.parse("type = aws_instance").eval(ms)
    assert not MatchQuery.parse("type = aws_lambda_function").eval(ms)
    assert MatchQuery.parse("os").eval(ms)  # key existence
    assert not MatchQuery.parse("region").eval(ms)


def test_match_query_boolean_precedence_and_parens():
    ms = _ms(("a", "1"), ("b", "2"))
    # OR < AND < NOT: `a = 1 and b = 3 or a = 1` -> (a=1 and b=3) or a=1 -> True
    assert MatchQuery.parse("a = 1 and b = 3 or a = 1").eval(ms)
    assert not MatchQuery.parse("a = 1 and b = 3").eval(ms)
    assert MatchQuery.parse("not b = 3").eval(ms)
    assert MatchQuery.parse("(a = 1 or a = 9) and b = 2").eval(ms)
    assert not MatchQuery.parse("not (a = 1)").eval(ms)


def test_match_query_empty_matches_everything():
    assert MatchQuery.parse("").eval(_ms())
    assert MatchQuery.parse("   ").eval(_ms(("x", "y")))


def test_match_query_real_usage_entry_with_not_and_parens():
    # From the default usage catalogue (Lambda x86 arch entry).
    q = MatchQuery.parse(
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


def _yaml_product(match="type=aws_instance", price_type="t"):
    return {
        "service": "AmazonEC2",
        "family": "Compute",
        "match": match,
        "pricing": "region=us-east-1",
        "price": "0.10",
        "price_type": price_type,
    }


def test_product_from_yaml_price_types():
    assert product_from_yaml(_yaml_product(price_type="t"), "USD").price.kind is PriceKind.PER_TIME
    assert (
        product_from_yaml(_yaml_product(price_type="o"), "USD").price.kind
        is PriceKind.PER_OPERATION
    )
    assert product_from_yaml(_yaml_product(price_type="d"), "USD").price.kind is PriceKind.PER_DATA
    attr = product_from_yaml(_yaml_product(price_type="a=values.storage"), "USD")
    assert attr.price.kind is PriceKind.ATTR and attr.price.attr == "values.storage"


def test_product_from_yaml_empty_match_set_skipped():
    with pytest.raises(EmptyMatchSet):
        product_from_yaml(_yaml_product(match=""), "USD")


def test_load_pricesheet_reads_terrapod_yaml():
    # the YAML format pricegen publishes (#893).
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
    # Terrapod's default usage catalogue: 18 base entries + additions — RDS
    # provisioned IOPS
    # io1/io2 (#928) + Azure Linux VM hours (#931) + GCP Compute hours (#933) +
    # NAT gateway hours+data (#966) + Elastic IP hours (#973) + load balancer
    # hours+LCU (#977) + classic ELB hours+data (#979) + EBS snapshot storage
    # (#981) + EFS storage (#983) + Aurora cluster instance hours (#985) +
    # KMS key + Secrets Manager secret + SNS publishes (#987) + Azure Windows VM
    # + Azure public IP (#989) + GCP persistent disk + GCP static IP (#991) +
    # API Gateway REST + HTTP requests (#993) + ElastiCache nodes (#995) +
    # Azure AKS hours + Azure storage account data (#997) + GCS bucket storage
    # (#999) + GKE cluster management (#1001) + Kinesis shards (#1003) + VPC
    # interface endpoint hours+data (#1005) + Azure managed disk per-size tier
    # (#1007) + Route53 hosted zone (#1009) + CloudFront data transfer (#1011) + ECR image storage (#1013) + Cloud DNS zone (#1015) + Azure ACR tier (#1017) + Pub/Sub throughput (#1019) + Cloud SQL vCPU/RAM/storage (#1021) + Azure PG vCore (#1023) + EBS provisioned IOPS io1/io2/gp3 (#929).
    assert len(entries) == 60
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
        match_query=MatchQuery.parse(""),
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
        match_query=MatchQuery.parse(""),
        usage=Usage.from_data_range(Range(900, 900)),
    )
    got = bound_to_usage_amount(DATA, Range(0, 100), point)
    assert got is not None and got.usage.data == Range(100, 100)
    # tier entirely above usage -> None
    assert bound_to_usage_amount(DATA, Range(2000, 3000), entry) is None


def test_apply_usage_amount_mixed_group_does_not_crash():
    """Regression (#1028 cost audit finding 1): a group where SOME products carry
    usage-amount tier bounds and some don't (the aws_lb/aws_elb shape — a flat
    PER_TIME hour sharing one entry with tiered PER_DATA bands) must NOT index a
    missing key (find_by_key → None → None[1] → TypeError, uncaught by price()).
    The untiered product is priced as the remainder instead."""
    from terrapod.services.cost.pricer import _apply_usage_amount
    from terrapod.services.cost.prices import product_from_yaml
    from terrapod.services.cost.usage import Entry, Usage

    tiered = product_from_yaml(
        {
            "service": "s",
            "family": "f",
            "match": "type=aws_lb",
            "price_type": "d",
            "price": "0.008",
            "pricing": "region=us-east-1&start_usage_amount=0&end_usage_amount=10000",
        },
        "USD",
    )
    flat = product_from_yaml(  # no usage-amount bounds → the missing-key crash source
        {
            "service": "s",
            "family": "f",
            "match": "type=aws_lb",
            "price_type": "t",
            "price": "0.0225",
            "pricing": "region=us-east-1",
        },
        "USD",
    )
    entry = Entry(
        description="d",
        divisor=None,
        match_query=MatchQuery.parse(""),
        usage=Usage.from_data_range(Range(0, 5000)),
    )
    out = _apply_usage_amount(entry, [tiered, flat])  # must not raise TypeError
    priced = [p for _, group in out for p in group]
    assert flat in priced and tiered in priced  # both accounted for


def test_price_products_zero_divisor_does_not_crash():
    """Regression (#1028 cost audit finding 2): a usage entry with divisor=0 must be
    treated as divisor 1 (parity with _count_factor's `or 1`), not ZeroDivisionError."""
    from terrapod.services.cost.pricer import _price_products
    from terrapod.services.cost.prices import product_from_yaml
    from terrapod.services.cost.usage import Entry, Usage

    product = product_from_yaml(
        {
            "service": "s",
            "family": "f",
            "match": "type=aws_x",
            "price_type": "d",
            "price": "0.10",
            "pricing": "region=us-east-1",
        },
        "USD",
    )
    entry = Entry(
        description="d",
        divisor=0,
        match_query=MatchQuery.parse(""),
        usage=Usage.from_data_range(Range(100, 100)),
    )
    result = _price_products(entry, [product])  # must not raise ZeroDivisionError
    assert result is not None


def test_range_intersect():
    assert intersect(Range(0, 5), Range(3, 10)) == Range(3, 5)
    assert intersect(Range(0, 5), Range(6, 10)) is None


def test_range_add_and_subtract_fold_both_bounds():
    assert Range(1, 2) + Range(10, 20) == Range(11, 22)
    assert Range(10, 20) - Range(1, 2) == Range(9, 18)


# ---------------------------------------------------------------------------
# end-to-end (hand-verified totals)
# ---------------------------------------------------------------------------

# One on-demand Linux EC2 instance. The EC2-hours usage default is time=730,
# the product is $0.10/hour Per_time, divisor 1 -> 730 * 0.10 = $73.00/month.
_EC2_ROW = (
    "AmazonEC2,Compute Instance,type=aws_instance,"
    "service_class=instance&purchase_option=on_demand&os=linux&region=us-east-1,"
    "0.1000000000,t,USD"
)


def _yaml_sheet(*rows: str) -> io.StringIO:
    """Build a Terrapod YAML pricesheet stream from comma-delimited
    ``service,product_family,match,pricing,price,price_type,ccy`` rows."""
    import yaml

    keys = ("service", "family", "match", "pricing", "price", "price_type")
    products = [dict(zip(keys, row.split(","), strict=False)) for row in rows]
    doc = {"schema": "terrapod-pricesheet/v1", "currency": "USD", "products": products}
    return io.StringIO(yaml.safe_dump(doc, sort_keys=False))


def _sheet() -> io.StringIO:
    return _yaml_sheet(_EC2_ROW)


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
_MULTI_REGION_ROWS = (
    "AmazonEC2,Compute,type=aws_instance,"
    "service_class=instance&purchase_option=on_demand&os=linux&region=us-east-1,0.10,t,USD",
    "AmazonEC2,Compute,type=aws_instance,"
    "service_class=instance&purchase_option=on_demand&os=linux&region=eu-west-1,0.20,t,USD",
)


def test_estimate_prices_each_resource_in_its_own_region():
    plan = _plan(
        resources=[
            _res("aws_instance.us", "aws_instance", "us", {"region": "us-east-1"}),
            _res("aws_instance.eu", "aws_instance", "eu", {"region": "eu-west-1"}),
        ]
    )
    est = estimate(plan, _yaml_sheet(*_MULTI_REGION_ROWS))
    by_addr = {r.address: r for r in est.resources}
    # us-east-1 @ $0.10/hr * 730 = $73; eu-west-1 @ $0.20/hr * 730 = $146.
    assert by_addr["aws_instance.us"].monthly_min == pytest.approx(73.0)
    assert by_addr["aws_instance.us"].monthly_max == pytest.approx(73.0)
    assert by_addr["aws_instance.eu"].monthly_min == pytest.approx(146.0)
    assert by_addr["aws_instance.eu"].monthly_max == pytest.approx(146.0)
    assert est.total_min == pytest.approx(219.0)
