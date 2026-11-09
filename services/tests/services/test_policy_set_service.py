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
    ctx = policy_set_service.build_run_context(ws, run, "plan")
    assert ctx["workspace"]["name"] == "prod-vpc"
    assert ctx["workspace"]["labels"] == {"env": "prod"}
    assert ctx["run"]["is_destroy"] is False
    assert ctx["run"]["plan_only"] is False
    assert ctx["run"]["message"] == "apply VPC"
    assert ctx["run"]["phase"] == "plan"


@pytest.mark.parametrize("phase", ["plan", "configure"])
def test_the_phase_reaches_the_policy_input(phase: str) -> None:
    """A policy must be able to tell which Job is being judged.

    Both Jobs of a COMPOSITE run (apply-then-configure) fetch one bundle for
    one run, so every other field in this document is identical between them.
    If the phase does not reach the input, a rule written for the playbook
    fires on the terraform plan as well, and vice versa.
    """
    ws = SimpleNamespace(id=uuid.uuid4(), name="w", labels={})
    run = SimpleNamespace(
        id=uuid.uuid4(),
        message="",
        source="",
        is_destroy=False,
        plan_only=False,
        # A composite carries this for BOTH Jobs, which is why it cannot be the
        # discriminator -- asserted here so the claim is pinned, not just said.
        is_configure=True,
    )
    ctx = policy_set_service.build_run_context(ws, run, phase)
    assert ctx["run"]["phase"] == phase
    assert "is_configure" not in ctx["run"]


def test_no_phase_is_an_empty_string_not_a_missing_key() -> None:
    """A Rego `input.terrapod_context.run.phase` must always resolve.

    An absent key makes a comparison undefined rather than false, so a rule
    guarded on the phase would silently not fire at all -- the fail-open shape
    this whole change exists to remove.
    """
    ws = SimpleNamespace(id=uuid.uuid4(), name="w", labels={})
    run = SimpleNamespace(id=uuid.uuid4(), message="", source="", is_destroy=False, plan_only=False)
    ctx = policy_set_service.build_run_context(ws, run, None)
    assert ctx["run"]["phase"] == ""


# ── evaluate_post_plan (one branch — others covered alongside the runner-protocol HTTP tests in test_policy_runner_endpoints.py) ──


@pytest.mark.asyncio
async def test_evaluate_post_plan_speculative_runs_never_block() -> None:
    """A plan-only run is informational; the gate must always pass even
    if the runner recorded a mandatory failure (the UI shows it, but
    there's no apply to block). The plan-only short-circuit lives at
    the top of the gate, so this test doesn't need the workspace,
    applicable-sets, or row-evidence machinery the other gate tests
    exercise in test_policy_runner_endpoints.py."""
    run = SimpleNamespace(id=uuid.uuid4(), plan_only=True)
    db = MagicMock()  # untouched on the plan-only branch
    with patch.object(
        policy_set_service, "run_is_policy_blocked", new=AsyncMock(return_value=True)
    ) as gate_query:
        assert (
            await policy_set_service.evaluate_post_plan(db, run) == policy_set_service.GATE_PASSED
        )
    gate_query.assert_not_called()


class TestWhyNoPolicySetWasEvaluated:
    """`policy_sets_not_evaluated_reason` is ENGINE-shaped, not run-shaped.

    It was run-shaped for one release and that was the defect. The class used
    to assert the opposite -- that a configure carries a reason -- and the
    reasoning was sound FOR ITS TIME: before #2435 a configure was not gated at
    all, and an operator who scoped a mandatory set to one saw nothing anywhere
    saying so, which is worse than the missing enforcement because the absence
    is invisible.

    #2435 delivered the enforcement, and the reason then became false in the
    one direction that matters: it told an operator a configure was ungated
    while the runner was gating it. Worse, two call sites act on it rather than
    merely reporting it, and both fail OPEN -- see the comments on the function.
    So the half of the old reasoning that survives is "make the absence
    visible"; what changed is that there is no longer an absence to make
    visible.
    """

    @staticmethod
    def _ws(engine: str = "terraform") -> SimpleNamespace:
        return SimpleNamespace(id=uuid.uuid4(), engine=engine)

    @staticmethod
    def _run(*, is_configure: bool, composite: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            plan_only=False,
            is_configure=is_configure,
            configure_after_apply=composite,
        )

    def test_no_run_shape_carries_a_reason(self) -> None:
        """All three legal run shapes, because each reached a different bug.

        A STANDALONE configure got an empty policy bundle, so #2435's in-Job
        gate evaluated nothing. A COMPOSITE got that AND skipped the post-plan
        safety net on its apply half, because it carries `is_configure=True`
        while genuinely having a plan. An ordinary run was always fine, and is
        here so a fix that simply inverted the predicate cannot pass.
        """
        ws = self._ws()
        for label, run in (
            ("standalone configure", self._run(is_configure=True)),
            ("composite", self._run(is_configure=True, composite=True)),
            ("ordinary plan/apply", self._run(is_configure=False)),
        ):
            assert policy_set_service.policy_sets_not_evaluated_reason(ws, run) is None, label

    def test_the_engine_answer_is_unchanged_when_no_run_is_given(self) -> None:
        # The pre-existing contract: a caller with no run in hand still gets
        # the engine-only answer rather than an exception.
        assert policy_set_service.policy_sets_not_evaluated_reason(self._ws()) is None

    def test_an_engine_that_produces_no_plan_json_still_carries_a_reason(self) -> None:
        """The reason that remains, and it is about the ENGINE.

        Kept for every run shape: an engine whose runner produces no plan JSON
        has nothing for a plan-shaped policy to read whatever the run is doing.
        """
        with patch("terrapod.engines.evaluates_policy_sets", return_value=False):
            for run in (
                self._run(is_configure=True),
                self._run(is_configure=True, composite=True),
                self._run(is_configure=False),
            ):
                reason = policy_set_service.policy_sets_not_evaluated_reason(
                    self._ws("pulumi"), run
                )
                assert reason is not None and "pulumi" in reason
