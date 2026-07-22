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
    # Azure Linux VM (#931) — proves the second cloud prices through the same
    # engine. size = armSkuName; region resolved from the resource's `location`.
    "Virtual Machines,Virtual Machines,type=azurerm_linux_virtual_machine&values.size=Standard_D2s_v5,service_provider=azure&purchase_option=on_demand&region=eastus&service_class=instance&os=linux&start_usage_amount=0&end_usage_amount=Inf,0.096,t,USD",
    # GCP compute (#933) — the computed kind: this row is pre-assembled from the
    # per-vCPU-core + per-GiB-RAM rates (n2-standard-4 = 4*core + 16*ram). Region
    # is derived from the resource's `zone`.
    "Compute Engine,Compute Instance,type=google_compute_instance&values.machine_type=n2-standard-4,service_provider=gcp&purchase_option=on_demand&region=us-central1&service_class=instance&start_usage_amount=0&end_usage_amount=Inf,0.1942360000,t,USD",
    # SQS — first request tier for a standard (fifo=false) and a FIFO queue. The
    # CORRECT mapping: FIFO is pricier ($0.50/M) than Standard ($0.40/M); oiq has
    # these inverted, so this row set deliberately does NOT match oiq.
    "AWSQueueService,API Request,type=aws_sqs_queue&values.fifo_queue=false,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=requests&start_usage_amount=0&end_usage_amount=100000000000,0.0000004000,o,USD",
    "AWSQueueService,API Request,type=aws_sqs_queue&values.fifo_queue=true,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=requests&start_usage_amount=0&end_usage_amount=100000000000,0.0000005000,o,USD",
    # Lambda — requests (arch-independent) + duration first tier, x86 and arm64.
    # x86 duration carries no architectures constraint (default lambda); arm64
    # carries values.architectures=arm64. arch pricing dim gates the usage entry.
    "AWSLambda,Serverless,type=aws_lambda_function,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=requests&start_usage_amount=0&end_usage_amount=Inf,0.0000002000,o,USD",
    "AWSLambda,Serverless,type=aws_lambda_function,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=duration&arch=x86&start_usage_amount=0&end_usage_amount=6000000000,0.0000166667,t,USD",
    "AWSLambda,Serverless,type=aws_lambda_function&values.architectures=arm64,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=duration&arch=arm64&start_usage_amount=0&end_usage_amount=7500000000,0.0000133334,t,USD",
    # S3 — Standard storage (first GB tier) + Tier-1 (PUT/LIST) + Tier-2 (GET)
    # requests, each correlated with a usage entry via a literal pricing dim.
    "AmazonS3,Storage,type=aws_s3_bucket,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage&storage_class=general_purpose&start_usage_amount=0&end_usage_amount=51200,0.0230000000,d,USD",
    "AmazonS3,API Request,type=aws_s3_bucket,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=requests&tier=1&start_usage_amount=0&end_usage_amount=Inf,0.0000050000,o,USD",
    "AmazonS3,API Request,type=aws_s3_bucket,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=requests&tier=2&start_usage_amount=0&end_usage_amount=Inf,0.0000004000,o,USD",
    # DynamoDB (PayPerRequest, Standard) — read + write request units + storage.
    # Storage starts at 0 (the 25 GB account-wide free tier is NOT applied
    # per-table), so a table is billed for its full storage.
    "AmazonDynamoDB,Amazon DynamoDB PayPerRequest Throughput,type=aws_dynamodb_table,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=requests&request_type=read&table_class=standard&start_usage_amount=0&end_usage_amount=Inf,0.0000001250,o,USD",
    "AmazonDynamoDB,Amazon DynamoDB PayPerRequest Throughput,type=aws_dynamodb_table,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=requests&request_type=write&table_class=standard&start_usage_amount=0&end_usage_amount=Inf,0.0000006250,o,USD",
    "AmazonDynamoDB,Database Storage,type=aws_dynamodb_table,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage&table_class=standard&start_usage_amount=0&end_usage_amount=Inf,0.2500000000,d,USD",
    # EBS gp3 storage — priced a=values.size. From the ~450 MB EC2 offer, now in
    # the published sheet via the ijson stream-filter (#893).
    "AmazonEC2,Storage,type=aws_ebs_volume&values.type=gp3,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage,0.0800000000,a=values.size,USD",
    # NAT gateway — deterministic always-on hours + usage-driven data processed.
    "AmazonEC2,NAT Gateway,type=aws_nat_gateway,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=hours&start_usage_amount=0&end_usage_amount=Inf,0.0450000000,t,USD",
    "AmazonEC2,NAT Gateway,type=aws_nat_gateway,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=data&start_usage_amount=0&end_usage_amount=Inf,0.0450000000,d,USD",
    # Elastic IP — every public IPv4 is $0.005/hr since 2024-02-01, so an aws_eip
    # is a deterministic always-on charge (from the AmazonVPC offer; the product's
    # empty productFamily falls back to the `group`).
    "AmazonVPC,VPCPublicIPv4Address,type=aws_eip,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=hours&start_usage_amount=0&end_usage_amount=Inf,0.0050000000,t,USD",
    # Load balancers — ALB + NLB, each a deterministic hour + a usage-driven LCU
    # (capacity units) band. load_balancer_type is stamped (schema Default, so a
    # plan always carries it). ALB LCU $0.008, NLB LCU $0.006.
    "AmazonEC2,Load Balancer-Application,type=aws_lb&values.load_balancer_type=application,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=hours&start_usage_amount=0&end_usage_amount=Inf,0.0225000000,t,USD",
    "AmazonEC2,Load Balancer-Application,type=aws_lb&values.load_balancer_type=application,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=lcu&start_usage_amount=0&end_usage_amount=Inf,0.0080000000,t,USD",
    "AmazonEC2,Load Balancer-Network,type=aws_lb&values.load_balancer_type=network,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=hours&start_usage_amount=0&end_usage_amount=Inf,0.0225000000,t,USD",
    "AmazonEC2,Load Balancer-Network,type=aws_lb&values.load_balancer_type=network,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=lcu&start_usage_amount=0&end_usage_amount=Inf,0.0060000000,t,USD",
    # Classic ELB (aws_elb) — deterministic hour + usage-driven data processed.
    "AmazonEC2,Load Balancer,type=aws_elb,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=hours&start_usage_amount=0&end_usage_amount=Inf,0.0250000000,t,USD",
    "AmazonEC2,Load Balancer,type=aws_elb,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=data&start_usage_amount=0&end_usage_amount=Inf,0.0080000000,d,USD",
    # EBS snapshot — usage-driven incremental stored size at $0.05/GB-month.
    "AmazonEC2,Storage Snapshot,type=aws_ebs_snapshot,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage&start_usage_amount=0&end_usage_amount=Inf,0.0500000000,d,USD",
    # EFS — usage-driven General Purpose stored size at $0.30/GB-month.
    "AmazonEFS,Storage,type=aws_efs_file_system,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage&start_usage_amount=0&end_usage_amount=Inf,0.3000000000,d,USD",
    # Aurora cluster instance (aws_rds_cluster_instance) — deterministic per-hour,
    # standard mode. r6g.large $0.26 (MySQL/PostgreSQL price identically -> one
    # deduped row keyed on instance_class only).
    "AmazonRDS,Database Instance,type=aws_rds_cluster_instance&values.instance_class=db.r6g.large,service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=instance&start_usage_amount=0&end_usage_amount=Inf,0.2600000000,t,USD",
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


def test_azure_linux_vm_prices_through_the_same_engine():
    """A second cloud: azurerm_linux_virtual_machine prices via size=armSkuName,
    with region resolved from the resource's `location`. Proves the provider-
    agnostic sheet + engine work beyond AWS (#931)."""
    plan = {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "azurerm_linux_virtual_machine.a",
                        "type": "azurerm_linux_virtual_machine",
                        "name": "a",
                        "mode": "managed",
                        "values": {"size": "Standard_D2s_v5", "location": "eastus"},
                    }
                ]
            }
        },
    }
    est = estimate(plan, _sheet())
    # 0.096/hr * 730 = 70.08
    assert est.resources[0].monthly_min == pytest.approx(0.096 * 730, abs=0.01)
    assert est.unpriced == []


def test_gcp_compute_instance_prices_with_zone_derived_region():
    """Third cloud: google_compute_instance prices via a pre-computed
    machine_type row, with region DERIVED from the resource's `zone`
    (us-central1-a -> us-central1). Proves the computed kind + zone handling
    end-to-end (#933)."""
    plan = {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "google_compute_instance.a",
                        "type": "google_compute_instance",
                        "name": "a",
                        "mode": "managed",
                        "values": {
                            "machine_type": "n2-standard-4",
                            "zone": "us-central1-a",
                        },
                    }
                ]
            }
        },
    }
    est = estimate(plan, _sheet())
    # 0.194236/hr * 730 = 141.79
    assert est.resources[0].monthly_min == pytest.approx(0.194236 * 730, abs=0.01)
    assert est.unpriced == []


def test_sqs_queue_fifo_is_pricier_than_standard():
    """SQS request pricing, and the correctness check: FIFO ($0.50/M) costs more
    than Standard ($0.40/M). oiq has these inverted; we map by the real AWS
    meaning, so this asserts the RIGHT answer, not oiq's (#893)."""
    plan = {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_sqs_queue.std",
                        "type": "aws_sqs_queue",
                        "name": "std",
                        "mode": "managed",
                        "values": {"fifo_queue": False, "region": "us-east-1"},
                    },
                    {
                        "address": "aws_sqs_queue.fifo",
                        "type": "aws_sqs_queue",
                        "name": "fifo",
                        "mode": "managed",
                        "values": {"fifo_queue": True, "region": "us-east-1"},
                    },
                ]
            }
        },
    }
    est = estimate(plan, _sheet())
    costs = {r.address: r.monthly_min for r in est.resources}
    # 50M requests/mo (the usage default) in the first tier.
    assert costs["aws_sqs_queue.std"] == pytest.approx(50_000_000 * 0.0000004, abs=0.01)
    assert costs["aws_sqs_queue.fifo"] == pytest.approx(50_000_000 * 0.0000005, abs=0.01)
    assert costs["aws_sqs_queue.fifo"] > costs["aws_sqs_queue.std"]  # FIFO pricier


def test_lambda_x86_and_arm64_price_by_architecture():
    """A default (x86) lambda and an arm64 lambda price their duration at the
    right per-arch rate — the `arch` pricing dimension + `values.architectures`
    correlate correctly (arm64 is cheaper). Requests are arch-independent (#893)."""

    def fn(addr, arch=None):
        v = {"region": "us-east-1", "memory_size": 512}
        if arch:
            v["architectures"] = [arch]
        return {
            "address": addr,
            "type": "aws_lambda_function",
            "name": addr.split(".")[-1],
            "mode": "managed",
            "values": v,
        }

    plan = {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "planned_values": {
            "root_module": {
                "resources": [fn("aws_lambda_function.d"), fn("aws_lambda_function.a", "arm64")]
            }
        },
    }
    est = estimate(plan, _sheet())
    costs = {r.address: r.monthly_min for r in est.resources}
    # usage defaults: 100M requests + 1M GB-s duration.
    reqs = 100_000_000 * 0.0000002
    assert costs["aws_lambda_function.d"] == pytest.approx(
        reqs + 1_000_000 * 0.0000166667, abs=0.01
    )
    assert costs["aws_lambda_function.a"] == pytest.approx(
        reqs + 1_000_000 * 0.0000133334, abs=0.01
    )
    assert costs["aws_lambda_function.a"] < costs["aws_lambda_function.d"]  # arm64 cheaper


def test_s3_bucket_storage_plus_request_tiers():
    """S3 prices its three dimensions — Standard storage (per-GB, first tier) +
    Tier-1 and Tier-2 requests — each correlated to its usage entry by a literal
    pricing dimension (storage_class / tier). (#893)"""
    plan = _plan(
        [
            {
                "address": "aws_s3_bucket.b",
                "type": "aws_s3_bucket",
                "name": "b",
                "mode": "managed",
                "values": {"region": "us-east-1"},
            }
        ]
    )
    est = estimate(plan, _sheet())
    # usage defaults: 900 GB storage + 4M Tier-1 + 50M Tier-2 requests.
    expected = 900 * 0.023 + 4_000_000 * 0.000005 + 50_000_000 * 0.0000004
    assert est.resources[0].monthly_min == pytest.approx(expected, abs=0.01)  # $60.70
    assert est.unpriced == []


def test_dynamodb_table_read_write_storage():
    """DynamoDB PayPerRequest table: read + write request units + full storage
    (the 25 GB account-wide free tier is deliberately NOT applied per-table, so
    it's consistent with Lambda and doesn't under-estimate). (#893)"""
    plan = _plan(
        [
            {
                "address": "aws_dynamodb_table.t",
                "type": "aws_dynamodb_table",
                "name": "t",
                "mode": "managed",
                "values": {"region": "us-east-1", "billing_mode": "PAY_PER_REQUEST"},
            }
        ]
    )
    est = estimate(plan, _sheet())
    # usage defaults: 80M reads + 16M writes + 80 GB storage (billed from 0).
    expected = 80_000_000 * 0.000000125 + 16_000_000 * 0.000000625 + 80 * 0.25
    assert est.resources[0].monthly_min == pytest.approx(expected, abs=0.01)  # $40.00
    assert est.unpriced == []


def test_ebs_gp3_volume_prices_by_size():
    """aws_ebs_volume gp3 storage is priced a=values.size (rate x the volume's
    size attr). Now in the published sheet via the EC2-offer stream-filter."""
    plan = _plan(
        [
            {
                "address": "aws_ebs_volume.d",
                "type": "aws_ebs_volume",
                "name": "d",
                "mode": "managed",
                "values": {"type": "gp3", "size": 100, "region": "us-east-1"},
            }
        ]
    )
    est = estimate(plan, _sheet())
    assert est.resources[0].monthly_min == pytest.approx(100 * 0.08, abs=0.01)  # $8.00


def test_usage_assumptions_flagged_with_bands_for_usage_driven_resources():
    """A usage-driven resource (Lambda) exposes usage_assumptions with
    low/typical/high bands so the AI/UI can flag + refine them; a deterministic
    resource (an EC2 instance — always-on hours) exposes none (#962)."""
    plan = _plan(
        [
            {
                "address": "aws_lambda_function.f",
                "type": "aws_lambda_function",
                "name": "f",
                "mode": "managed",
                "values": {"region": "us-east-1", "memory_size": 512},
            },
            {
                "address": "aws_instance.web",
                "type": "aws_instance",
                "name": "web",
                "mode": "managed",
                "values": {"instance_type": "m5.large", "region": "us-east-1"},
            },
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    by_addr = {r["address"]: r for r in d["resources"]}
    lam = by_addr["aws_lambda_function.f"]
    dims = {ua["dimension"]: ua for ua in lam["usage_assumptions"]}
    assert "invocations" in dims and "duration" in dims
    inv = dims["invocations"]
    assert inv["low"] < inv["typical"] < inv["high"]  # a real quantity band
    assert inv["unit"] and inv["description"]
    # Each band also carries its monthly dollar impact at low/typical/high (#962).
    assert inv["cost_low"] < inv["cost_typical"] < inv["cost_high"]
    assert inv["cost_low"] >= 0
    # deterministic EC2 instance: no usage assumptions, field omitted.
    assert "usage_assumptions" not in by_addr["aws_instance.web"]


def test_nat_gateway_deterministic_hours_plus_usage_driven_data():
    """NAT gateway: the always-on hour is deterministic (confident), the data
    processed is usage-driven and flagged with a band — the full #962 loop on a
    net-new resource. (#893/#962)"""
    plan = _plan(
        [
            {
                "address": "aws_nat_gateway.n",
                "type": "aws_nat_gateway",
                "name": "n",
                "mode": "managed",
                "values": {"region": "us-east-1"},
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    # hours 730*0.045=32.85 (deterministic) + data 100*0.045=4.50 (typical)
    assert r["monthly"]["min"] == pytest.approx(730 * 0.045 + 100 * 0.045, abs=0.01)
    dims = {ua["dimension"]: ua for ua in r["usage_assumptions"]}
    assert set(dims) == {"data processed"}  # only data is an assumption, not hours
    data = dims["data processed"]
    assert data["low"] < data["high"]
    # Per-item COST band (#962): the monthly dollar impact at low/typical/high
    # usage. data rate 0.045/GB → 10→$0.45, 100→$4.50, 50000→$2250.
    assert data["cost_low"] == pytest.approx(10 * 0.045, abs=0.01)
    assert data["cost_typical"] == pytest.approx(100 * 0.045, abs=0.01)
    assert data["cost_high"] == pytest.approx(50000 * 0.045, abs=0.01)
    # The headline monthly stays at typical (hours + typical data), NOT the
    # widened band — so workspace totals don't balloon on the high bound.
    assert r["monthly"]["max"] == pytest.approx(730 * 0.045 + data["cost_typical"], abs=0.01)


def test_eip_deterministic_hourly_public_ipv4():
    """aws_eip: since 2024-02-01 every public IPv4 is billed $0.005/hr whether
    attached or idle, so an Elastic IP is a deterministic always-on charge with
    NO usage assumption (like the NAT hour). 0.005*730 = $3.65/mo. (#893)"""
    plan = _plan(
        [
            {
                "address": "aws_eip.nat",
                "type": "aws_eip",
                "name": "nat",
                "mode": "managed",
                "values": {"region": "us-east-1", "domain": "vpc"},
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    assert r["monthly"]["min"] == pytest.approx(730 * 0.005, abs=0.01)
    assert r["monthly"]["max"] == pytest.approx(730 * 0.005, abs=0.01)
    # Deterministic — no usage band, field omitted entirely.
    assert "usage_assumptions" not in r


def test_alb_hours_plus_lcu_band():
    """aws_lb (application): deterministic hour ($0.0225 * 730) + usage-driven
    LCU capacity band. load_balancer_type is stamped, so the ALB rows match and
    the NLB rows don't. (#893/#977)"""
    plan = _plan(
        [
            {
                "address": "aws_lb.app",
                "type": "aws_lb",
                "name": "app",
                "mode": "managed",
                "values": {"region": "us-east-1", "load_balancer_type": "application"},
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    # hours 730*0.0225 = 16.425 + LCU typical 1460*0.008 = 11.68 -> 28.105
    assert r["monthly"]["max"] == pytest.approx(730 * 0.0225 + 1460 * 0.008, abs=0.01)
    dims = {ua["dimension"]: ua for ua in r["usage_assumptions"]}
    assert set(dims) == {"capacity units"}  # LCU is the assumption, not the hour
    lcu = dims["capacity units"]
    assert lcu["cost_typical"] == pytest.approx(1460 * 0.008, abs=0.01)
    assert lcu["cost_high"] == pytest.approx(30000 * 0.008, abs=0.01)  # honest busy high


def test_nlb_prices_at_the_network_lcu_rate():
    """aws_lb (network) picks the NLB family rows (LCU $0.006, not the ALB
    $0.008) via the stamped load_balancer_type. (#977)"""
    plan = _plan(
        [
            {
                "address": "aws_lb.net",
                "type": "aws_lb",
                "name": "net",
                "mode": "managed",
                "values": {"region": "us-east-1", "load_balancer_type": "network"},
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    assert r["monthly"]["max"] == pytest.approx(730 * 0.0225 + 1460 * 0.006, abs=0.01)
    lcu = {ua["dimension"]: ua for ua in r["usage_assumptions"]}["capacity units"]
    assert lcu["cost_typical"] == pytest.approx(1460 * 0.006, abs=0.01)


def test_classic_elb_hours_plus_data_band():
    """aws_elb (classic): deterministic hour ($0.025 * 730) + usage-driven data
    processed band ($0.008/GB). (#893/#979)"""
    plan = _plan(
        [
            {
                "address": "aws_elb.legacy",
                "type": "aws_elb",
                "name": "legacy",
                "mode": "managed",
                "values": {"region": "us-east-1"},
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    # hours 730*0.025 = 18.25 + data typical 500*0.008 = 4.0 -> 22.25
    assert r["monthly"]["max"] == pytest.approx(730 * 0.025 + 500 * 0.008, abs=0.01)
    dims = {ua["dimension"]: ua for ua in r["usage_assumptions"]}
    assert set(dims) == {"data processed"}
    data = dims["data processed"]
    assert data["cost_typical"] == pytest.approx(500 * 0.008, abs=0.01)
    assert data["cost_high"] == pytest.approx(50000 * 0.008, abs=0.01)


def test_ebs_snapshot_usage_driven_storage_band():
    """aws_ebs_snapshot: the billed incremental size is unknowable from the plan,
    so it's a usage-driven storage band at $0.05/GB-month. (#893/#981)"""
    plan = _plan(
        [
            {
                "address": "aws_ebs_snapshot.s",
                "type": "aws_ebs_snapshot",
                "name": "s",
                "mode": "managed",
                "values": {"region": "us-east-1"},
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    # typical 100 GB * 0.05 = $5.00
    assert r["monthly"]["max"] == pytest.approx(100 * 0.05, abs=0.01)
    data = {ua["dimension"]: ua for ua in r["usage_assumptions"]}["stored size"]
    assert data["cost_typical"] == pytest.approx(100 * 0.05, abs=0.01)
    assert data["cost_high"] == pytest.approx(16000 * 0.05, abs=0.01)


def test_efs_usage_driven_storage_band():
    """aws_efs_file_system: General Purpose stored size is usage-driven ($0.30/
    GB-month) — EFS grows with the data written, not a plan attribute. (#983)"""
    plan = _plan(
        [
            {
                "address": "aws_efs_file_system.fs",
                "type": "aws_efs_file_system",
                "name": "fs",
                "mode": "managed",
                "values": {"region": "us-east-1"},
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    assert r["monthly"]["max"] == pytest.approx(100 * 0.30, abs=0.01)  # typical 100 GB
    data = {ua["dimension"]: ua for ua in r["usage_assumptions"]}["stored size"]
    assert data["cost_typical"] == pytest.approx(100 * 0.30, abs=0.01)
    assert data["cost_high"] == pytest.approx(50000 * 0.30, abs=0.01)


def test_aurora_cluster_instance_prices_deterministically():
    """aws_rds_cluster_instance (Aurora): deterministic per-hour, standard mode.
    Matched on instance_class only (engine omitted — MySQL/PostgreSQL price the
    same and dedup to one row). No usage band. (#893/#985)"""
    plan = _plan(
        [
            {
                "address": "aws_rds_cluster_instance.writer",
                "type": "aws_rds_cluster_instance",
                "name": "writer",
                "mode": "managed",
                "values": {
                    "region": "us-east-1",
                    "instance_class": "db.r6g.large",
                    "engine": "aurora-postgresql",
                },
            }
        ]
    )
    d = estimate(plan, _sheet()).to_dict()
    r = d["resources"][0]
    assert r["monthly"]["max"] == pytest.approx(730 * 0.26, abs=0.01)  # $189.80
    assert "usage_assumptions" not in r  # deterministic


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
