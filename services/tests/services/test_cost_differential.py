"""Differential oracle: native cost engine vs the real ``oiq`` binary (#871).

This is the correctness backstop for the ported engine. It runs the genuine
OpenInfraQuote binary over a plan/state corpus and asserts our native Python
engine agrees bit-exact (within a cent of float noise). **The binary is never
shipped** — it lives only here, in CI, as an oracle: production carries no
third-party cost binary.

The test skips unless both are available in the environment:

* ``OIQ_BINARY``    — path to (or name on ``PATH`` of) the ``oiq`` executable.
* ``OIQ_PRICESHEET`` — path to a ``prices.csv`` for the binary to price against
  (the *same* sheet is fed to the native engine, so the assertion is engine
  agreement, not agreement with a fixed dollar figure — the corpus never pins
  amounts, so a daily pricesheet refresh moves both engines together).
* ``OIQ_REGION`` (optional, default ``us-east-1``) — the single region the
  whole corpus is priced against. See ``cost_corpus/README.md``: oiq prices a
  plan against a per-plan ``--region``, whereas Terrapod resolves region
  per-resource, so the corpus is single-region to keep this an apples-to-apples
  test of the shared matcher + pricer.

The dedicated CI job (``Cost Differential``) sets these. Locally and in the
default test container they are unset, so this skips — the hand-verified
fixtures in ``test_cost_engine.py`` cover the offline path.

The ``oiq`` invocation and JSON shape below were pinned against the real
``oiq v1.10.0`` binary (``oiq match --pricesheet S FILE | oiq price
--format json --region R``); ``price`` emits ``{price, prev_price,
price_diff}`` each a ``{min, max}`` range plus a per-resource ``resources[]``.
The parsing fails loudly (not silently) if a future ``oiq`` changes that shape,
so the oracle can be corrected rather than rubber-stamp.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_CORPUS = Path(__file__).parent / "cost_corpus"
_TOLERANCE = 0.01  # absolute USD tolerance on monthly min/max (float noise only)
_REGION = os.environ.get("OIQ_REGION", "us-east-1")


def _oiq_binary() -> str | None:
    env = os.environ.get("OIQ_BINARY")
    if env:
        return env if os.path.isabs(env) else shutil.which(env)
    return shutil.which("oiq")


def _corpus_files() -> list[Path]:
    if not _CORPUS.is_dir():
        return []
    return sorted(_CORPUS.glob("*.json"))


_BINARY = _oiq_binary()
_PRICESHEET = os.environ.get("OIQ_PRICESHEET")

pytestmark = pytest.mark.skipif(
    not (_BINARY and _PRICESHEET and Path(_PRICESHEET).is_file()),
    reason="differential oracle needs OIQ_BINARY + OIQ_PRICESHEET (set in the CI oracle job)",
)


def _oiq_price(plan_path: Path) -> dict[str, tuple[float, float]]:
    """Run ``oiq match | oiq price`` and return the total/prev/diff ranges.

    Returns a dict of ``{"total"|"prev"|"diff": (min, max)}`` — the whole-plan
    figures the native engine's ``CostEstimate`` mirrors.
    """
    assert _BINARY and _PRICESHEET
    matched = subprocess.run(
        [_BINARY, "match", "--pricesheet", _PRICESHEET, str(plan_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    priced = subprocess.run(
        # --format json: the default is a human "summary"; --region: sugar for
        # 'not region or region=R', pricing the plan in one region (see module
        # docstring + corpus README on why the corpus is single-region).
        [_BINARY, "price", "--format", "json", "--region", _REGION],
        input=matched.stdout,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(priced.stdout)
    # Fail loudly if the pinned shape ever drifts, rather than silently passing.
    for key in ("price", "prev_price", "price_diff"):
        if key not in data or "min" not in data[key] or "max" not in data[key]:
            raise AssertionError(
                f"oiq price JSON shape changed: missing {key}.min/max in {sorted(data)}"
            )
    return {
        "total": (float(data["price"]["min"]), float(data["price"]["max"])),
        "prev": (float(data["prev_price"]["min"]), float(data["prev_price"]["max"])),
        "diff": (float(data["price_diff"]["min"]), float(data["price_diff"]["max"])),
    }


@pytest.mark.parametrize("plan_path", _corpus_files(), ids=lambda p: p.name)
def test_native_engine_matches_oiq(plan_path: Path):
    from terrapod.services.cost import estimate

    oiq = _oiq_price(plan_path)
    with plan_path.open() as fp:
        tf_json = json.load(fp)
    with open(_PRICESHEET) as sheet:  # type: ignore[arg-type]
        est = estimate(tf_json, sheet, default_region=_REGION)

    native = {
        "total": (est.total_min, est.total_max),
        "prev": (est.prev_min, est.prev_max),
        "diff": (est.diff_min, est.diff_max),
    }
    for field in ("total", "prev", "diff"):
        assert native[field][0] == pytest.approx(oiq[field][0], abs=_TOLERANCE), (
            f"{plan_path.name}: {field}.min native={native[field][0]} oiq={oiq[field][0]}"
        )
        assert native[field][1] == pytest.approx(oiq[field][1], abs=_TOLERANCE), (
            f"{plan_path.name}: {field}.max native={native[field][1]} oiq={oiq[field][1]}"
        )


def test_corpus_present_when_oracle_enabled():
    """Guard: with the oracle enabled, the corpus must be non-empty."""
    assert _corpus_files(), "differential oracle enabled but cost_corpus/ is empty"
