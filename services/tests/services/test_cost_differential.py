"""Differential oracle: native cost engine vs the real ``oiq`` binary (#871).

This is the correctness backstop for the ported engine. It runs the genuine
OpenInfraQuote binary over a plan/state corpus and asserts our native Python
engine agrees within tolerance. **The binary is never shipped** — it lives only
here, in CI, as an oracle: production carries no third-party cost binary.

The test skips unless both are available in the environment:

* ``OIQ_BINARY``   — path to (or name on ``PATH`` of) the ``oiq`` executable.
* ``OIQ_PRICESHEET`` — path to a ``prices.csv`` for the binary to price against
  (the same sheet is fed to the native engine).

The dedicated CI job sets both. Locally and in the default test container they
are unset, so this skips — the hand-verified fixtures in ``test_cost_engine.py``
cover the offline path. Corpus fixtures live under
``services/tests/services/cost_corpus/`` (``*.json`` terraform show output);
the directory is optional until the corpus lands.

NOTE: the exact ``oiq`` CLI invocation + output shape is pinned by the CI job
that first runs this against a real binary; the parsing below targets the
documented ``match | price`` pipeline and fails loudly (not silently) if the
observed shape differs, so the oracle can be corrected rather than rubber-stamp.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_CORPUS = Path(__file__).parent / "cost_corpus"
_TOLERANCE = 0.01  # absolute USD tolerance on monthly min/max


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


def _oiq_price(plan_path: Path) -> tuple[float, float]:
    """Run ``oiq match | oiq price`` and return the monthly (min, max) total."""
    assert _BINARY and _PRICESHEET
    match = subprocess.run(
        [_BINARY, "match", "--pricesheet", _PRICESHEET, str(plan_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    priced = subprocess.run(
        [_BINARY, "price"],
        input=match.stdout,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(priced.stdout)
    price = data["price"]
    return (float(price["min"]), float(price["max"]))


@pytest.mark.parametrize("plan_path", _corpus_files(), ids=lambda p: p.name)
def test_native_engine_matches_oiq(plan_path: Path):
    from terrapod.services.cost import estimate

    oiq_min, oiq_max = _oiq_price(plan_path)
    with plan_path.open() as fp:
        tf_json = json.load(fp)
    with open(_PRICESHEET) as sheet:  # type: ignore[arg-type]
        est = estimate(tf_json, sheet)
    assert est.total_min == pytest.approx(oiq_min, abs=_TOLERANCE), plan_path.name
    assert est.total_max == pytest.approx(oiq_max, abs=_TOLERANCE), plan_path.name


def test_corpus_present_when_oracle_enabled():
    """Guard: with the oracle enabled, the corpus must be non-empty."""
    assert _corpus_files(), "differential oracle enabled but cost_corpus/ is empty"
