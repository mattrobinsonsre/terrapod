"""Tests for the runner-side OPA policy evaluation (#343).

The runner entrypoint's ``tp_evaluate_policies`` function takes an OPA
``opa eval --format json`` output, extracts ``deny`` / ``warn`` from a
deeply nested structure, and coerces scalar values to arrays so a
mis-authored ``deny := true`` doesn't silently pass. The defensive
jq pipeline is the load-bearing piece of the runner side; this test
extracts that pipeline from ``docker/runner-entrypoint.sh`` and exercises
it directly against representative OPA outputs.

Skips cleanly when ``jq`` isn't on PATH so the suite runs in
environments without jq. The Docker test image installs jq, so CI
exercises the real pipeline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_JQ = shutil.which("jq") is not None
needs_jq = pytest.mark.skipif(not _JQ, reason="jq not on PATH")


def _entrypoint_path() -> Path:
    """Locate ``docker/runner-entrypoint.sh`` from either layout."""
    here = Path(__file__).resolve()
    for cand in (here.parents[3] / "docker", here.parents[2] / "docker"):
        path = cand / "runner-entrypoint.sh"
        if path.is_file():
            return path
    pytest.skip("runner-entrypoint.sh not reachable from the test environment")
    raise AssertionError("unreachable: pytest.skip() raises Skipped")  # pragma: no cover


def _extract_deny_jq() -> str:
    """Pull the defensive deny-extraction jq expression out of the
    entrypoint. Tied to a structural marker (the ``_deny=$(echo
    "$_opa_out" | jq '…')`` line) so an unrelated edit doesn't silently
    invalidate the test.
    """
    text = _entrypoint_path().read_text()
    match = re.search(
        r"_deny=\$\(echo \"\$_opa_out\" \| jq '([^']+)'\)",
        text,
    )
    if match is None:
        raise AssertionError(
            "Could not locate the deny-extraction jq expression in "
            "runner-entrypoint.sh — the marker structure has changed; "
            "update this test."
        )
    return match.group(1)


def _run_jq(expression: str, value: object) -> list[str]:
    """Run ``jq <expression>`` against an OPA-shaped ``opa eval`` doc
    and return the resulting array."""
    if shutil.which("jq") is None:
        pytest.skip("jq not available")
    result = subprocess.run(
        ["jq", expression],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(result.stdout)
    assert isinstance(out, list), f"expression must return a list, got {out!r}"
    return out


def _opa_envelope(value: object) -> dict:
    """Wrap a `data.terrapod` value in the OPA `eval --format json`
    envelope shape that the runner actually receives."""
    return {"result": [{"expressions": [{"value": value}]}]}


# ── B2: the jq pipeline coerces every shape OPA can serialise ──────


@needs_jq
def test_partial_set_deny_extracts_messages() -> None:
    """The normal case: `deny contains msg if {...}` produces a set."""
    out = _run_jq(
        _extract_deny_jq(),
        _opa_envelope({"deny": ["bucket is public", "no encryption"]}),
    )
    assert out == ["bucket is public", "no encryption"]


@needs_jq
def test_scalar_string_deny_is_coerced() -> None:
    """A misauthored `deny := "msg"` would have errored the old jq
    pipeline; the defensive coercion wraps the scalar in a single-
    element array so the violation is still recorded."""
    out = _run_jq(_extract_deny_jq(), _opa_envelope({"deny": "blocked"}))
    assert out == ["blocked"]


@needs_jq
def test_scalar_true_deny_is_coerced() -> None:
    """B2 reviewer case: ``deny := true`` would have errored the old jq
    pipeline (cannot iterate over a boolean), then the assignment in
    bash would have ended up empty, and the policy would silently
    pass despite the would-be denial. The coercion records ``"true"``."""
    out = _run_jq(_extract_deny_jq(), _opa_envelope({"deny": True}))
    assert out == ["true"]


@needs_jq
def test_undefined_deny_returns_empty() -> None:
    """A policy in `package terrapod` that doesn't define `deny` at all
    — the value lacks the key entirely."""
    out = _run_jq(_extract_deny_jq(), _opa_envelope({}))
    assert out == []


@needs_jq
def test_empty_query_result_returns_empty() -> None:
    """OPA returns ``{"result": []}`` when no rule in the queried
    package matches. The jq pipeline must not error trying to index
    ``.result[0].expressions``."""
    out = _run_jq(_extract_deny_jq(), {"result": []})
    assert out == []


@needs_jq
def test_array_deny_is_sorted() -> None:
    """Deterministic ordering of violation messages — surfaces stable
    diffs to the operator across re-evaluations."""
    out = _run_jq(
        _extract_deny_jq(),
        _opa_envelope({"deny": ["zeta", "alpha", "mike"]}),
    )
    assert out == ["alpha", "mike", "zeta"]
