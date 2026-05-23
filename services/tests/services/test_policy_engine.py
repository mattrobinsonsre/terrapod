"""Tests for the OPA policy evaluation engine (#343).

The output-parsing helpers are pure and always run. The tests that
exercise the real ``opa`` binary skip when it is not on PATH (it is
installed in docker/Dockerfile.test, so they run in CI).
"""

from __future__ import annotations

import json
import shutil

import pytest

from terrapod.services import policy_engine

_OPA = shutil.which("opa") is not None
needs_opa = pytest.mark.skipif(not _OPA, reason="opa binary not on PATH")

PASS_POLICY = """
package terrapod

deny contains msg if {
    false
    msg := "never"
}
"""

DENY_POLICY = """
package terrapod

deny contains msg if {
    input.foo == "bar"
    msg := "foo must not be bar"
}
"""

WARN_POLICY = """
package terrapod

warn contains msg if {
    input.foo == "bar"
    msg := "foo is bar — heads up"
}
"""

CONTEXT_POLICY = """
package terrapod

deny contains msg if {
    data.terrapod_context.workspace.name == "blocked-ws"
    msg := "workspace is on the blocklist"
}
"""

BROKEN_POLICY = "package terrapod\n\ndeny contains msg if { ::: }\n"


# ── Pure helpers ──────────────────────────────────────────────────────


def test_string_list_coerces_and_sorts() -> None:
    assert policy_engine._string_list(["b", "a", "c"]) == ["a", "b", "c"]
    assert policy_engine._string_list([1, 2]) == ["1", "2"]


def test_string_list_non_list_is_empty() -> None:
    assert policy_engine._string_list(None) == []
    assert policy_engine._string_list("not a list") == []
    assert policy_engine._string_list({"a": 1}) == []


def test_parse_opa_output_extracts_deny_and_warn() -> None:
    doc = {"result": [{"expressions": [{"value": {"deny": ["v1", "v2"], "warn": ["w1"]}}]}]}
    res = policy_engine._parse_opa_output("p", json.dumps(doc).encode())
    assert res.violations == ["v1", "v2"]
    assert res.warnings == ["w1"]
    assert res.passed is False


def test_parse_opa_output_empty_result_is_pass() -> None:
    # A query that matched no package rules — clean pass, not an error.
    res = policy_engine._parse_opa_output("p", json.dumps({"result": []}).encode())
    assert res.passed is True
    assert res.violations == []
    assert res.error is None


def test_parse_opa_output_unparseable_is_error() -> None:
    res = policy_engine._parse_opa_output("p", b"not json")
    assert res.passed is False
    assert res.error is not None


# ── Real OPA evaluation ───────────────────────────────────────────────


@needs_opa
async def test_evaluate_passing_policy() -> None:
    res = await policy_engine.evaluate_policy("pass", PASS_POLICY, b"{}", {})
    assert res.passed is True
    assert res.violations == []
    assert res.error is None


@needs_opa
async def test_evaluate_denying_policy() -> None:
    plan = json.dumps({"foo": "bar"}).encode()
    res = await policy_engine.evaluate_policy("deny", DENY_POLICY, plan, {})
    assert res.passed is False
    assert res.violations == ["foo must not be bar"]


@needs_opa
async def test_evaluate_policy_warnings_do_not_fail() -> None:
    plan = json.dumps({"foo": "bar"}).encode()
    res = await policy_engine.evaluate_policy("warn", WARN_POLICY, plan, {})
    assert res.passed is True
    assert res.warnings == ["foo is bar — heads up"]


@needs_opa
async def test_evaluate_policy_reads_terrapod_context() -> None:
    context = {"workspace": {"name": "blocked-ws"}}
    res = await policy_engine.evaluate_policy("ctx", CONTEXT_POLICY, b"{}", context)
    assert res.passed is False
    assert res.violations == ["workspace is on the blocklist"]


@needs_opa
async def test_evaluate_broken_rego_is_error() -> None:
    res = await policy_engine.evaluate_policy("broken", BROKEN_POLICY, b"{}", {})
    assert res.passed is False
    assert res.error is not None


@needs_opa
async def test_check_rego_accepts_valid() -> None:
    assert await policy_engine.check_rego(PASS_POLICY) is None


@needs_opa
async def test_check_rego_rejects_broken() -> None:
    err = await policy_engine.check_rego(BROKEN_POLICY)
    assert err is not None
    # The internal temp path must not leak into the error message.
    assert "/tmp/tp-policy-" not in err
