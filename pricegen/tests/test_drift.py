"""Unit tests for the drift guardrail (#922).

check_drift is pure — synthetic prev/curr manifests + a small health config,
no offer files. It's the scheduled-job logic that decides whether a freshly
generated sheet is safe to publish over the last good one.
"""

from __future__ import annotations

from pricegen.drift import check_drift


def _recipe(name, rows, unmapped=None, pmax=100.0):
    return {
        "recipe": name,
        "rows": rows,
        "unmapped": unmapped or {},
        "price_min": 0.1,
        "price_max": pmax,
    }


def _manifest(*recipes):
    return {"recipes": list(recipes)}


_HEALTH = {
    "min_rows": {"aws_db_instance": 1000, "gcp": 50},
    "row_delta_fraction": 0.5,
    "price_swing_factor": 10,
    "known_unmapped": {"aws_db_instance": {"match.engine": ["Aurora", "Db2"]}},
}


def test_no_drift_is_clean():
    m = _manifest(_recipe("aws_db_instance", 3200))
    r = check_drift(m, m, _HEALTH)
    assert not r.blocked and r.findings == []


def test_coverage_collapse_below_floor_blocks():
    prev = _manifest(_recipe("aws_db_instance", 3200))
    curr = _manifest(_recipe("aws_db_instance", 0))  # family regex stopped matching
    r = check_drift(prev, curr, _HEALTH)
    assert r.blocked
    assert "floor" in r.findings[0].message


def test_recipe_vanished_blocks():
    prev = _manifest(_recipe("aws_db_instance", 3200), _recipe("gcp", 90))
    curr = _manifest(_recipe("aws_db_instance", 3200))
    r = check_drift(prev, curr, _HEALTH)
    assert r.blocked
    assert any(f.recipe == "gcp" and "vanished" in f.message for f in r.findings)


def test_new_unmapped_value_warns():
    prev = _manifest(_recipe("aws_db_instance", 3200))
    curr = _manifest(
        _recipe("aws_db_instance", 3200, unmapped={"match.engine": {"Neptune|": 5}})
    )
    r = check_drift(prev, curr, _HEALTH)
    assert not r.blocked  # a warn, not a block
    assert any("Neptune" in f.message and f.severity == "warn" for f in r.findings)


def test_known_unmapped_value_is_allowed():
    prev = _manifest(_recipe("aws_db_instance", 3200))
    # Aurora + Db2 are on the allowlist -> no warning.
    curr = _manifest(
        _recipe(
            "aws_db_instance",
            3200,
            unmapped={"match.engine": {"Aurora MySQL|": 3, "Db2|Standard": 2}},
        )
    )
    r = check_drift(prev, curr, _HEALTH)
    assert r.findings == []


def test_unmapped_value_seen_last_publish_does_not_re_warn():
    prev = _manifest(
        _recipe("aws_db_instance", 3200, unmapped={"match.engine": {"Weird|": 1}})
    )
    curr = _manifest(
        _recipe("aws_db_instance", 3200, unmapped={"match.engine": {"Weird|": 1}})
    )
    r = check_drift(prev, curr, _HEALTH)
    assert r.findings == []  # not new -> no warn


def test_large_row_move_warns():
    prev = _manifest(_recipe("aws_db_instance", 3200))
    curr = _manifest(_recipe("aws_db_instance", 1500))  # -53%, above floor but big move
    r = check_drift(prev, curr, _HEALTH)
    assert not r.blocked
    assert any("row count moved" in f.message for f in r.findings)


def test_price_swing_warns():
    prev = _manifest(_recipe("aws_db_instance", 3200, pmax=100.0))
    curr = _manifest(_recipe("aws_db_instance", 3200, pmax=2000.0))  # 20x
    r = check_drift(prev, curr, _HEALTH)
    assert any("price swung" in f.message for f in r.findings)


def test_issue_body_groups_by_severity():
    prev = _manifest(_recipe("aws_db_instance", 3200), _recipe("gcp", 90))
    curr = _manifest(
        _recipe("aws_db_instance", 0, unmapped={"match.engine": {"Neptune|": 1}})
    )
    body = check_drift(prev, curr, _HEALTH).issue_body()
    assert "Blocking" in body and "Warnings" in body
    assert "gcp" in body and "aws_db_instance" in body
