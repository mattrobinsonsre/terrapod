"""Tests for policy-set scoping + evaluation orchestration (#343)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services import policy_set_service
from terrapod.services.policy_engine import PolicyResult


def _ps(**kw) -> SimpleNamespace:
    """A policy-set stub — attribute reads only, no DB."""
    base = {
        "enabled": True,
        "global_scope": False,
        "allow_labels": {},
        "allow_names": [],
        "deny_labels": {},
        "deny_names": [],
        "name": "set",
        "enforcement_level": "advisory",
        "policies": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ── _labels_match ─────────────────────────────────────────────────────


def test_labels_match_scalar_value() -> None:
    assert policy_set_service._labels_match({"env": "prod"}, {"env": "prod"}) is True


def test_labels_match_list_of_accepted_values() -> None:
    assert (
        policy_set_service._labels_match({"env": "staging"}, {"env": ["prod", "staging"]}) is True
    )


def test_labels_match_no_match() -> None:
    assert policy_set_service._labels_match({"env": "dev"}, {"env": ["prod"]}) is False


def test_labels_match_key_absent() -> None:
    assert policy_set_service._labels_match({"team": "infra"}, {"env": "prod"}) is False


def test_labels_match_empty_rule() -> None:
    assert policy_set_service._labels_match({"env": "prod"}, {}) is False


# ── policy_set_applies ────────────────────────────────────────────────


def test_disabled_set_never_applies() -> None:
    ps = _ps(enabled=False, global_scope=True)
    assert policy_set_service.policy_set_applies(ps, "w", {}) is False


def test_global_set_applies_to_everything() -> None:
    ps = _ps(global_scope=True)
    assert policy_set_service.policy_set_applies(ps, "anything", {"env": "dev"}) is True


def test_allow_label_match_applies() -> None:
    ps = _ps(allow_labels={"env": ["prod"]})
    assert policy_set_service.policy_set_applies(ps, "w", {"env": "prod"}) is True
    assert policy_set_service.policy_set_applies(ps, "w", {"env": "dev"}) is False


def test_allow_name_match_applies() -> None:
    ps = _ps(allow_names=["special-ws"])
    assert policy_set_service.policy_set_applies(ps, "special-ws", {}) is True


def test_deny_takes_precedence_over_allow() -> None:
    ps = _ps(allow_labels={"env": ["prod"]}, deny_labels={"tier": ["sandbox"]})
    # Matches allow, but also matches deny → denied.
    assert (
        policy_set_service.policy_set_applies(ps, "w", {"env": "prod", "tier": "sandbox"}) is False
    )


def test_deny_name_excludes() -> None:
    ps = _ps(allow_labels={"env": ["prod"]}, deny_names=["excluded-ws"])
    assert policy_set_service.policy_set_applies(ps, "excluded-ws", {"env": "prod"}) is False


def test_no_rule_match_does_not_apply() -> None:
    ps = _ps(allow_labels={"env": ["prod"]})
    assert policy_set_service.policy_set_applies(ps, "w", {"team": "infra"}) is False


# ── _evaluate_one_set outcome aggregation ─────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_one_set_all_pass() -> None:
    ps = _ps(policies=[SimpleNamespace(name="a", rego="x"), SimpleNamespace(name="b", rego="y")])
    with patch.object(
        policy_set_service.policy_engine,
        "evaluate_policy",
        new=AsyncMock(return_value=PolicyResult("a", passed=True)),
    ):
        outcome, result = await policy_set_service._evaluate_one_set(ps, b"{}", {})
    assert outcome == "passed"
    assert len(result["policies"]) == 2


@pytest.mark.asyncio
async def test_evaluate_one_set_violation_fails() -> None:
    ps = _ps(policies=[SimpleNamespace(name="a", rego="x")])
    with patch.object(
        policy_set_service.policy_engine,
        "evaluate_policy",
        new=AsyncMock(return_value=PolicyResult("a", passed=False, violations=["nope"])),
    ):
        outcome, _ = await policy_set_service._evaluate_one_set(ps, b"{}", {})
    assert outcome == "failed"


@pytest.mark.asyncio
async def test_evaluate_one_set_error_takes_precedence() -> None:
    """A policy that errors outranks one that merely has violations —
    a mandatory set then fails closed."""
    ps = _ps(policies=[SimpleNamespace(name="a", rego="x"), SimpleNamespace(name="b", rego="y")])
    results = [
        PolicyResult("a", passed=False, violations=["nope"]),
        PolicyResult("b", passed=False, error="opa exploded"),
    ]
    with patch.object(
        policy_set_service.policy_engine,
        "evaluate_policy",
        new=AsyncMock(side_effect=results),
    ):
        outcome, _ = await policy_set_service._evaluate_one_set(ps, b"{}", {})
    assert outcome == "errored"
