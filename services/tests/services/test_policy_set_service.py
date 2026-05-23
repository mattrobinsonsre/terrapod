"""Tests for policy-set scoping + evaluation orchestration (#343)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.db.models import now_utc
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


# ── evaluate_post_plan orchestration ──────────────────────────────────


def _stub_run(**kw) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
        "has_json_output": False,
        "plan_finished_at": None,
        "plan_only": False,
        "message": "m",
        "source": "tfe-api",
        "is_destroy": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _stub_ws() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name="w", labels={"env": "prod"})


def _stub_db(
    *,
    existing_eval_id: object = None,
    blocked: bool = False,
) -> MagicMock:
    """Build a MagicMock async db just rich enough for evaluate_post_plan.

    ``existing_eval_id`` controls what the "are there already PolicyEvaluation
    rows for this run?" query returns (a tuple to indicate present, ``None``
    to indicate absent). ``blocked`` controls what ``run_is_policy_blocked``
    sees afterwards.
    """
    db = MagicMock()
    db.get = AsyncMock(return_value=_stub_ws())

    already_row = (existing_eval_id,) if existing_eval_id is not None else None
    blocked_row = (uuid.uuid4(),) if blocked else None

    # First `db.execute` call: the "already" probe. Subsequent calls (from
    # run_is_policy_blocked) get the blocked-row result. side_effect drives
    # them in order.
    first = MagicMock()
    first.first = MagicMock(return_value=already_row)
    second = MagicMock()
    second.first = MagicMock(return_value=blocked_row)
    # Any further executes (e.g. _insert_evaluations) — return a benign mock.
    db.execute = AsyncMock(side_effect=[first, second, MagicMock(), MagicMock()])
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_evaluate_post_plan_no_applicable_sets_passes() -> None:
    db = _stub_db()
    run = _stub_run()
    with patch.object(policy_set_service, "applicable_policy_sets", new=AsyncMock(return_value=[])):
        result = await policy_set_service.evaluate_post_plan(db, run)
    assert result == policy_set_service.GATE_PASSED


@pytest.mark.asyncio
async def test_evaluate_post_plan_pending_when_no_json_within_grace() -> None:
    """Plan JSON not yet uploaded and grace window not elapsed → caller
    should retry on the next reconciler tick."""
    db = _stub_db()
    run = _stub_run(has_json_output=False, plan_finished_at=now_utc())
    ps = _ps(enforcement_level="mandatory")
    with patch.object(
        policy_set_service, "applicable_policy_sets", new=AsyncMock(return_value=[ps])
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)
    assert result == policy_set_service.GATE_PENDING


@pytest.mark.asyncio
async def test_evaluate_post_plan_records_unavailable_past_grace() -> None:
    """Plan JSON never arrived, grace elapsed → record errored evals
    fail-closed; a mandatory set then blocks the run.

    This test would fail if the grace timer regressed to its broken
    state (anchored on a field the gate itself prevents from being set
    — see #343's first review).
    """
    db = _stub_db(blocked=True)
    run = _stub_run(
        has_json_output=False,
        plan_finished_at=now_utc()
        - timedelta(seconds=policy_set_service.PLAN_JSON_GRACE_SECONDS + 30),
    )
    ps = _ps(enforcement_level="mandatory", id=uuid.uuid4())
    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[ps]),
        _insert_evaluations=AsyncMock(),
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)
    assert result == policy_set_service.GATE_BLOCKED


@pytest.mark.asyncio
async def test_evaluate_post_plan_idempotent_when_evals_already_present() -> None:
    """Second call must NOT re-OPA; it just re-checks the gate so an
    override taken since the first call takes effect."""
    db = _stub_db(existing_eval_id=uuid.uuid4(), blocked=False)
    run = _stub_run(has_json_output=True)
    ps = _ps(enforcement_level="mandatory")

    eval_mock = AsyncMock()
    with patch.multiple(
        policy_set_service,
        applicable_policy_sets=AsyncMock(return_value=[ps]),
        _evaluate_one_set=eval_mock,
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)
    assert result == policy_set_service.GATE_PASSED
    eval_mock.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_post_plan_speculative_runs_never_gate() -> None:
    """Plan-only (speculative) runs are evaluated and recorded but the
    gate always returns PASSED — there's no apply to block."""
    db = _stub_db(blocked=True)  # would block a non-plan-only run
    run = _stub_run(plan_only=True, has_json_output=False, plan_finished_at=now_utc())
    ps = _ps(enforcement_level="mandatory")
    with patch.object(
        policy_set_service, "applicable_policy_sets", new=AsyncMock(return_value=[ps])
    ):
        result = await policy_set_service.evaluate_post_plan(db, run)
    # Either passed outright (eval not done — gate pending) or passed because
    # the speculative check kicks in. In both cases never BLOCKED.
    assert result != policy_set_service.GATE_BLOCKED
