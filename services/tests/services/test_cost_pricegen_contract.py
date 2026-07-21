"""End-to-end pricing contract for pricegen-shaped rows (#926).

pricegen (#893) generates the pricesheet; this asserts that rows in the shape
pricegen actually emits **price correctly through the real consumer engine** —
the guard that row-parity-against-oiq could not give us. It embeds a small
curated sheet in pricegen's exact output shape and runs synthetic plans through
``estimate``.

The headline case is the #924 regression: a ``terraform plan`` for a new
postgres DB emits ``license_model`` as null/absent (Optional+Computed), so an
instance row that required it in its match_set silently failed to price. These
rows deliberately carry **no** ``license_model`` in the match; the postgres
plan below must still price its instance. If a future recipe change reintroduces
a fragile match key, this fails loudly.
"""

from __future__ import annotations

import io

import pytest

from terrapod.services.cost.engine import estimate

# --- a curated sheet in pricegen's exact output shape ----------------------
# Faithful to real generated rows (us-east-1). Instance rows carry NO
# license_model (the #924 fix); storage is priced a=values.allocated_storage
# and keyed only by (storage_type, multi_az) — engine-independent (#920).
_ROWS = [
    "service,product_family,match_set,pricing_match_set,price,price_type,ccy",
    # postgres db.t3.medium single-AZ instance — one license -> exact price.
    "AmazonRDS,Database Instance,type=aws_db_instance&values.engine=postgres&values.instance_class=db.t3.medium&values.multi_az=false,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=instance&start_usage_amount=0&end_usage_amount=Inf,0.0720000000,t,USD",
    # oracle-se2 db.t3.xlarge single-AZ — BYOL vs License-included, same key,
    # two prices -> a min..max RANGE when the plan doesn't pin the license.
    "AmazonRDS,Database Instance,type=aws_db_instance&values.engine=oracle-se2&values.instance_class=db.t3.xlarge&values.multi_az=false,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=instance&start_usage_amount=0&end_usage_amount=Inf,0.2720000000,t,USD",
    "AmazonRDS,Database Instance,type=aws_db_instance&values.engine=oracle-se2&values.instance_class=db.t3.xlarge&values.multi_az=false,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=instance&start_usage_amount=0&end_usage_amount=Inf,0.6000000000,t,USD",
    # gp2 storage single-AZ — engine-independent, priced by allocated_storage.
    "AmazonRDS,Database Storage,type=aws_db_instance&values.storage_type=gp2&values.multi_az=false,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage,0.1150000000,a=values.allocated_storage,USD",
    # gp2 storage multi-AZ — double rate.
    "AmazonRDS,Database Storage,type=aws_db_instance&values.storage_type=gp2&values.multi_az=true,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage,0.2300000000,a=values.allocated_storage,USD",
    # io1 storage + io1 provisioned IOPS — both priced from the resource's attrs.
    "AmazonRDS,Database Storage,type=aws_db_instance&values.storage_type=io1&values.multi_az=false,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage,0.1250000000,a=values.allocated_storage,USD",
    "AmazonRDS,Provisioned IOPS,type=aws_db_instance&values.storage_type=io1&values.multi_az=false,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=iops,0.1000000000,a=values.iops,USD",
    # EC2 for breadth.
    "AmazonEC2,Compute Instance,type=aws_instance&values.instance_type=m5.large,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=instance&os=linux&start_usage_amount=0&end_usage_amount=Inf,0.0960000000,t,USD",
]


def _sheet() -> io.StringIO:
    return io.StringIO("\n".join(_ROWS))


def _plan(resources):
    return {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "planned_values": {"root_module": {"resources": resources}},
    }


def _rds(addr, values):
    base = {"region": "us-east-1"}
    base.update(values)
    return {
        "address": addr,
        "type": "aws_db_instance",
        "name": addr.split(".")[-1],
        "mode": "managed",
        "values": base,
    }


# --- the #924 regression: postgres prices WITHOUT license_model ------------


def test_rds_postgres_prices_without_license_model():
    """A new-DB plan omits license_model (Optional+Computed). The instance MUST
    still price. Pre-#924 this returned storage-only ($11.50)."""
    plan = _plan(
        [
            _rds(
                "aws_db_instance.pg",
                {
                    "engine": "postgres",
                    "instance_class": "db.t3.medium",
                    "multi_az": False,
                    "storage_type": "gp2",
                    "allocated_storage": 100,
                },
            )
        ]
    )
    est = estimate(plan, _sheet())
    # instance 0.072*730=52.56 + storage 100*0.115=11.50 = 64.06
    assert est.resources[0].monthly_min == pytest.approx(64.06, abs=0.01)
    assert est.resources[0].monthly_max == pytest.approx(64.06, abs=0.01)
    assert est.unpriced == []


def test_rds_postgres_still_prices_when_license_model_present():
    """The License-included/BYOL-agnostic match also prices when a plan DOES
    carry license_model (state, or an explicitly-set plan)."""
    plan = _plan(
        [
            _rds(
                "aws_db_instance.pg",
                {
                    "engine": "postgres",
                    "instance_class": "db.t3.medium",
                    "multi_az": False,
                    "storage_type": "gp2",
                    "allocated_storage": 100,
                    "license_model": "postgresql-license",
                },
            )
        ]
    )
    est = estimate(plan, _sheet())
    assert est.resources[0].monthly_min == pytest.approx(64.06, abs=0.01)


# --- license range when the plan doesn't pin the license -------------------


def test_rds_oracle_prices_to_a_license_range():
    """oracle-se2 has BYOL and License-included at different prices under one
    key; with no license_model in the plan the consumer reports a min..max
    range — the honest answer, not a single wrong number."""
    plan = _plan(
        [
            _rds(
                "aws_db_instance.ora",
                {
                    "engine": "oracle-se2",
                    "instance_class": "db.t3.xlarge",
                    "multi_az": False,
                    "storage_type": "gp2",
                    "allocated_storage": 100,
                },
            )
        ]
    )
    est = estimate(plan, _sheet())
    r = est.resources[0]
    # instance range [0.272,0.600]*730 + storage 11.50
    assert r.monthly_min == pytest.approx(0.272 * 730 + 11.50, abs=0.01)
    assert r.monthly_max == pytest.approx(0.600 * 730 + 11.50, abs=0.01)
    assert r.monthly_max > r.monthly_min  # it IS a range


# --- storage: allocated_storage drives the cost; multi-AZ doubles ----------


def test_rds_storage_scales_with_allocated_storage():
    plan = _plan(
        [
            _rds(
                "aws_db_instance.big",
                {
                    "engine": "postgres",
                    "instance_class": "db.t3.medium",
                    "multi_az": False,
                    "storage_type": "gp2",
                    "allocated_storage": 500,
                },
            )
        ]
    )
    est = estimate(plan, _sheet())
    # storage 500*0.115=57.50 + instance 52.56 = 110.06
    assert est.resources[0].monthly_min == pytest.approx(52.56 + 57.50, abs=0.01)


def test_rds_storage_multi_az_doubles():
    plan = _plan(
        [
            _rds(
                "aws_db_instance.ha",
                {
                    "engine": "postgres",
                    "instance_class": "db.t3.medium",
                    "multi_az": True,
                    "storage_type": "gp2",
                    "allocated_storage": 100,
                },
            )
        ]
    )
    est = estimate(plan, _sheet())
    # multi-AZ storage 100*0.23=23.00; instance has no multi_az=true row here,
    # so only storage prices -> 23.00 (guards the multi_az storage discriminator).
    assert est.resources[0].monthly_min == pytest.approx(23.00, abs=0.01)


# --- provisioned IOPS: io1/io2 priced exactly from the iops attr (#928) -----


def test_rds_io1_provisioned_iops_prices_exactly():
    """io1 has no free baseline — every provisioned IOPS is billed. a=values.iops
    reads the real count, so io1 (and io2, same flat rate) price exactly. Before
    #928 this was $0 (no usage entry matched io1 iops)."""
    plan = _plan(
        [
            _rds(
                "aws_db_instance.io1",
                {
                    "engine": "postgres",
                    "instance_class": "db.t3.medium",
                    "multi_az": False,
                    "storage_type": "io1",
                    "allocated_storage": 100,
                    "iops": 5000,
                },
            )
        ]
    )
    est = estimate(plan, _sheet())
    # instance 52.56 + io1 storage 100*0.125=12.50 + iops 5000*0.10=500 = 565.06
    assert est.resources[0].monthly_min == pytest.approx(52.56 + 12.50 + 500.0, abs=0.01)


def test_rds_gp3_default_iops_is_free_baseline():
    """A gp3 volume with no explicit iops must NOT be charged for provisioned
    IOPS (its 3000 baseline is free). gp3 is excluded from the iops component,
    and a missing iops attr is caught — either way, no spurious IOPS charge."""
    plan = _plan(
        [
            _rds(
                "aws_db_instance.gp3",
                {
                    "engine": "postgres",
                    "instance_class": "db.t3.medium",
                    "multi_az": False,
                    "storage_type": "gp3",
                    "allocated_storage": 100,
                },
            )
        ]
    )
    est = estimate(plan, _sheet())
    # no gp3 storage row in this fixture -> instance-only 52.56; crucially no iops.
    assert est.resources[0].monthly_min == pytest.approx(52.56, abs=0.01)


# --- EC2 breadth + the unpriced bucket -------------------------------------


def test_ec2_instance_prices():
    plan = {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_instance.web",
                        "type": "aws_instance",
                        "name": "web",
                        "mode": "managed",
                        "values": {"instance_type": "m5.large", "region": "us-east-1"},
                    }
                ]
            }
        },
    }
    est = estimate(plan, _sheet())
    assert est.resources[0].monthly_min == pytest.approx(0.096 * 730, abs=0.01)
