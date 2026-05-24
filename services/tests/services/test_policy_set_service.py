"""Tests for policy-set scoping + the simplified post-plan gate (#343).

After the OPA-on-runner refactor, the API-side service exposes:

- pure scoping helpers (``_labels_match``, ``policy_set_applies``,
  ``applicable_policy_sets``) — exercised below
- a thin gate (``evaluate_post_plan``) that's now a DB query — exercised
  below
- a persistence helper (``_insert_evaluations``) — exercised via the
  HTTP endpoint tests in ``tests/api/test_policy_runner_endpoints.py``
- ``build_run_context`` — exercised via the runner-bundle endpoint test

The evaluation orchestration that used to live here is gone (it now
runs in the runner); the tests for it are gone with it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import policy_set_service


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


# ── build_run_context ────────────────────────────────────────────────


def test_build_run_context_shape() -> None:
    ws = SimpleNamespace(id=uuid.uuid4(), name="prod-vpc", labels={"env": "prod"})
    run = SimpleNamespace(
        id=uuid.uuid4(),
        message="apply VPC",
        source="tfe-api",
        is_destroy=False,
        plan_only=False,
    )
    ctx = policy_set_service.build_run_context(ws, run)
    assert ctx["workspace"]["name"] == "prod-vpc"
    assert ctx["workspace"]["labels"] == {"env": "prod"}
    assert ctx["run"]["is_destroy"] is False
    assert ctx["run"]["plan_only"] is False
    assert ctx["run"]["message"] == "apply VPC"


# ── evaluate_post_plan (simplified gate query) ────────────────────────


def _stub_run(*, plan_only: bool = False, bundle_fetched: bool = True) -> SimpleNamespace:
    """Run stub. By default the bundle was fetched — exercises the fast
    path. Set ``bundle_fetched=False`` to drive the rolling-upgrade
    safety branch."""
    from datetime import UTC, datetime

    fetched_at = datetime.now(UTC) if bundle_fetched else None
    return SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        plan_only=plan_only,
        policy_bundle_fetched_at=fetched_at,
    )


@pytest.mark.asyncio
async def test_evaluate_post_plan_speculative_runs_never_block() -> None:
    """A plan-only run is informational; the gate must always pass even
    if the runner recorded a mandatory failure (the UI shows it, but
    there's no apply to block)."""
    run = _stub_run(plan_only=True)
    # `db` is unused on the plan-only branch — pass a benign mock.
    db = MagicMock()
    with patch.object(
        policy_set_service, "run_is_policy_blocked", new=AsyncMock(return_value=True)
    ) as gate_query:
        assert (
            await policy_set_service.evaluate_post_plan(db, run) == policy_set_service.GATE_PASSED
        )
    gate_query.assert_not_called()


@pytest.mark.asyncio
async def test_evaluate_post_plan_blocked_when_mandatory_failure() -> None:
    run = _stub_run()
    db = MagicMock()
    with patch.object(
        policy_set_service, "run_is_policy_blocked", new=AsyncMock(return_value=True)
    ):
        assert (
            await policy_set_service.evaluate_post_plan(db, run) == policy_set_service.GATE_BLOCKED
        )


@pytest.mark.asyncio
async def test_evaluate_post_plan_passed_when_no_block() -> None:
    """Includes the 'no applicable sets / no runner results' case —
    ``run_is_policy_blocked`` returns False when no rows exist for the
    run."""
    run = _stub_run()
    db = MagicMock()
    with patch.object(
        policy_set_service, "run_is_policy_blocked", new=AsyncMock(return_value=False)
    ):
        assert (
            await policy_set_service.evaluate_post_plan(db, run) == policy_set_service.GATE_PASSED
        )
