"""SQLite pricesheet index — build + query (#1034).

Covers the `yaml.parse` event-streaming build (the core of #1034 — the whole
sheet is never materialised), the `(type, region)` candidate narrowing, and an
end-to-end `estimate()` through the YAML path.
"""

from __future__ import annotations

import io
import os
import tempfile

from terrapod.services.cost.engine import estimate
from terrapod.services.cost.pricesheet_db import PricesheetIndex, build_index

# A Terrapod YAML sheet exactly as pricegen emits it: schema, currency, then a
# `products:` list of flat mappings. Diverse types + regions.
_YAML = """\
schema: terrapod-pricesheet/v1
currency: USD
products:
- service: AmazonEC2
  family: Compute Instance
  match: type=aws_instance&values.instance_type=m5.large
  pricing: service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=instance&os=linux&start_usage_amount=0&end_usage_amount=Inf
  price: '0.0960000000'
  price_type: t
- service: AmazonEC2
  family: Compute Instance
  match: type=aws_instance&values.instance_type=m5.large
  pricing: service_provider=aws&purchase_option=on_demand&region=eu-west-1&service_class=instance&os=linux&start_usage_amount=0&end_usage_amount=Inf
  price: '0.1070000000'
  price_type: t
- service: AmazonS3
  family: Storage
  match: type=aws_s3_bucket
  pricing: service_provider=aws&purchase_option=on_demand&region=us-east-1&service_class=storage&start_usage_amount=0&end_usage_amount=51200
  price: '0.0230000000'
  price_type: d
"""


def test_yaml_event_build_extracts_type_region_currency():
    idx = PricesheetIndex.build_temp(io.StringIO(_YAML))
    # us-east-1 aws_instance → the us-east row (0.096); eu-west row excluded
    c = list(idx.candidates("aws_instance", "us-east-1"))
    assert len(c) == 1
    assert c[0].price.value == 0.096
    assert c[0].ccy == "USD"  # captured from the doc-level `currency`
    # eu-west-1 → the eu row (0.107)
    e = list(idx.candidates("aws_instance", "eu-west-1"))
    assert len(e) == 1 and e[0].price.value == 0.107


def test_candidates_narrow_by_type_and_region():
    idx = PricesheetIndex.build_temp(io.StringIO(_YAML))
    # a type with no products → nothing
    assert list(idx.candidates("aws_db_instance", "us-east-1")) == []
    # s3 is only in us-east-1 → empty in another region
    assert list(idx.candidates("aws_s3_bucket", "eu-west-1")) == []
    assert len(list(idx.candidates("aws_s3_bucket", "us-east-1"))) == 1


def test_estimate_end_to_end_through_yaml_event_path():
    # A multi-region plan priced through the yaml.parse build → correct per-region.
    state = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_instance.east",
                        "type": "aws_instance",
                        "name": "east",
                        "values": {"instance_type": "m5.large", "region": "us-east-1"},
                    },
                    {
                        "address": "aws_instance.euw",
                        "type": "aws_instance",
                        "name": "euw",
                        "values": {"instance_type": "m5.large", "region": "eu-west-1"},
                    },
                ]
            }
        }
    }
    est = estimate(state, io.StringIO(_YAML))
    by = {r.address: r.monthly_max for r in est.resources}
    # each instance priced at its region's rate (0.107 > 0.096 confirms multi-region)
    assert by["aws_instance.euw"] > by["aws_instance.east"] > 0
    # 730h/mo × 0.096 for the us-east instance
    assert abs(by["aws_instance.east"] - 730 * 0.096) < 0.5


def test_build_index_file_and_open():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        n = build_index(io.StringIO(_YAML), path)
        assert n == 3  # all three products indexed
        idx = PricesheetIndex.open(path)
        assert len(list(idx.candidates("aws_instance", "us-east-1"))) == 1
        idx.close()
    finally:
        os.unlink(path)


def test_index_estimate_matches_stream_estimate():
    # Passing a pre-built index yields the same result as passing the stream.
    state = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_instance.a",
                        "type": "aws_instance",
                        "name": "a",
                        "values": {"instance_type": "m5.large", "region": "us-east-1"},
                    }
                ]
            }
        }
    }
    via_stream = estimate(state, io.StringIO(_YAML))
    idx = PricesheetIndex.build_temp(io.StringIO(_YAML))
    via_index = estimate(state, index=idx)
    assert via_stream.total_max == via_index.total_max
