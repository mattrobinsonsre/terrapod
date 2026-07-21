"""Unit tests for the GCP computed-engine path (#933).

Deterministic — in-memory Core/Ram Units, no network. generate_computed reads
each family's per-vCPU-core + per-GiB-RAM rate and pre-computes each machine
type's total (vCPU x core + RAM_GiB x ram), emitting one values.machine_type
row per type.
"""

from __future__ import annotations

from pricegen.engine import RecipeStats, generate_computed
from pricegen.models import Price, Unit


def _core(fam, region, usd):
    return Unit(
        f"{fam} Instance Core running in Americas",
        {
            "description": f"{fam} Instance Core running in Americas",
            "usage_type": "OnDemand",
            "region": region,
        },
        [Price(usd=str(usd))],
    )


def _ram(fam, region, usd):
    return Unit(
        f"{fam} Instance Ram running in Americas",
        {
            "description": f"{fam} Instance Ram running in Americas",
            "usage_type": "OnDemand",
            "region": region,
        },
        [Price(usd=str(usd))],
    )


_DEFAULTS = {
    "canonical": {"usage_type": "OnDemand"},
    "pricing_common": {"service_provider": "gcp", "purchase_option": "on_demand"},
}


def _recipe():
    return {
        "resource_type": "google_compute_instance",
        "service": "Compute Engine",
        "computed": {
            "region": "us-central1",
            "family_regex": r"^(\w+)(?: Predefined)? Instance (Core|Ram) running in",
            "vcpus": [1, 4],
            "families": {"n2": {"standard": 4, "highmem": 8}},
        },
    }


def _rows(units, stats=None):
    return list(
        generate_computed(
            _recipe(), _DEFAULTS, units, service="Compute Engine", stats=stats
        )
    )


def test_computed_total_is_core_plus_ram():
    units = [_core("N2", "us-central1", 0.031611), _ram("N2", "us-central1", 0.004237)]
    rows = _rows(units)
    by_mt = {r[2].split("machine_type=")[1]: float(r[4]) for r in rows}
    # n2-standard-4 = 4*0.031611 + 16*0.004237 = 0.194236
    assert abs(by_mt["n2-standard-4"] - (4 * 0.031611 + 16 * 0.004237)) < 1e-9
    # n2-highmem-4 = 4*0.031611 + 32*0.004237
    assert abs(by_mt["n2-highmem-4"] - (4 * 0.031611 + 32 * 0.004237)) < 1e-9
    # n2-standard-1 = 1 core + 4 GiB
    assert abs(by_mt["n2-standard-1"] - (1 * 0.031611 + 4 * 0.004237)) < 1e-9
    # 2 shapes x 2 vcpus = 4 rows
    assert len(rows) == 4
    for r in rows:
        assert r[5] == "t" and r[6] == "USD"
        assert "service_provider=gcp" in r[3] and "region=us-central1" in r[3]


def test_computed_skips_other_regions_and_commitment():
    units = [
        _core("N2", "europe-west1", 0.9),  # wrong region
        _ram("N2", "europe-west1", 0.9),
        _core("N2", "us-central1", 0.031611),
        _ram("N2", "us-central1", 0.004237),
    ]
    rows = _rows(units)
    # only the us-central1 rates used
    n2s4 = next(float(r[4]) for r in rows if "n2-standard-4" in r[2])
    assert abs(n2s4 - (4 * 0.031611 + 16 * 0.004237)) < 1e-9


def test_computed_missing_family_rates_recorded_as_drift():
    # only Core present for n2 -> can't compute -> family flagged unmapped.
    stats = RecipeStats()
    rows = _rows([_core("N2", "us-central1", 0.031611)], stats=stats)
    assert rows == []
    assert stats.unmapped["family"]["n2"] == 1


def test_computed_ignores_custom_instance_meters():
    # "N2 Custom Instance Core" must not match the family_regex.
    units = [
        Unit(
            "N2 Custom Instance Core running in Americas",
            {
                "description": "N2 Custom Instance Core running in Americas",
                "usage_type": "OnDemand",
                "region": "us-central1",
            },
            [Price(usd="0.033")],
        ),
        _core("N2", "us-central1", 0.031611),
        _ram("N2", "us-central1", 0.004237),
    ]
    rows = _rows(units)
    # standard core rate used (0.031611), not the custom 0.033
    n2s1 = next(float(r[4]) for r in rows if "n2-standard-1" in r[2])
    assert abs(n2s1 - (0.031611 + 4 * 0.004237)) < 1e-9
