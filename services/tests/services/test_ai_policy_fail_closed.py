"""The three fail-closed paths that could stop every apply in a deployment.

The AI policy gate (#1766) holds a run when it has no verdict, deliberately:
silence is not consent. That is right, and it makes every path where a verdict
CANNOT arrive a fleet-wide outage rather than a missing panel. The v1.8.0
third-party review found three, and none of them needed a bug to trigger —
each was reachable from a documented, supported configuration.

These pin the fixes. Each is mutation-checked against the shipped behaviour.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import AIPolicyConfig, AISummaryConfig
from terrapod.services import ai_policy_service
from terrapod.services.summariser_prompt import render_prompt


def _prompt(**kw):
    base = {
        "kind": "plan_summary",
        "fleet_context": "",
        "workspace_context": "",
        "primary_input": "{}",
        "primary_input_label": "PLAN_JSON",
        "primary_input_lang": "json",
        "code_context_truncated": "",
    }
    base.update(kw)
    return render_prompt(**base)


# ── 1. a gate configured on the risk threshold alone ─────────────────


class TestAThresholdOnlyGateStillAsksForAVerdict:
    """`values.yaml` says of the two triggers: "use either or both". A gate
    with a threshold and no criteria took that at its word, and then the
    prompt was keyed on the criteria — so the model was never asked for a
    verdict, `decide_outcome` saw None, and EVERY run was recorded `errored`.
    Under `mandatory` that held every run in the deployment, with an error
    blaming the model for not answering a question nobody put to it."""

    def test_the_gate_section_renders_without_any_criteria(self):
        system, _ = _prompt(deny_criteria="", wants_verdict=True)
        assert "POLICY GATE" in system

    def test_the_model_is_told_what_to_do_with_no_criteria(self):
        """Left unsaid, a model handed a gate block and no criteria may well
        omit the field anyway — which is the original failure, restored."""
        system, _ = _prompt(deny_criteria="", wants_verdict=True)
        assert "no DENY_CRITERIA section is present" in system

    def test_criteria_still_reach_the_user_message(self):
        _, user = _prompt(deny_criteria="no public S3 buckets", wants_verdict=True)
        assert "DENY_CRITERIA:" in user
        assert "no public S3 buckets" in user

    def test_an_ungated_run_is_byte_identical_to_its_pre_gate_shape(self):
        """The gate must cost an ungated deployment nothing at all."""
        gated, _ = _prompt(wants_verdict=False)
        assert "POLICY GATE" not in gated
        assert "policy_verdict" not in gated

    def test_a_threshold_alone_configures_the_gate(self):
        """The predicate the prompt now keys on. If this drifts back to
        requiring criteria, the threshold-only deployment silently stops
        being gated at all — the opposite failure, and quieter."""
        with patch.object(
            ai_policy_service.settings,
            "ai_summary",
            SimpleNamespace(
                policy=SimpleNamespace(
                    enabled=True,
                    deny_criteria="",
                    risk_threshold="high",
                    enforcement_level="mandatory",
                )
            ),
        ):
            assert ai_policy_service.is_configured() is True


# ── 2. a mandatory gate with the summariser switched off ─────────────


class TestTheGateCannotOutliveItsSummariser:
    """The verdict is produced BY the summariser. With `ai_summary.enabled:
    false` nothing enqueues it, no verdict lands, and a mandatory gate holds
    every apply-capable run forever — keeping its workspace lock, re-driven
    by the reconciler on every tick, with the override answering 409.

    Nothing validated the combination, and `runbooks.md` told the on-call to
    create it: its AI-outage remedy was "set ai_summary.enabled: false"."""

    def test_the_catastrophic_combination_is_refused_at_load(self):
        with pytest.raises(ValueError, match="mandatory"):
            AISummaryConfig(
                enabled=False,
                policy=AIPolicyConfig(
                    enabled=True, enforcement_level="mandatory", risk_threshold="high"
                ),
            )

    def test_the_message_says_all_three_ways_out(self):
        """An operator hitting this at startup needs the fix, not a diagnosis."""
        with pytest.raises(ValueError) as exc:
            AISummaryConfig(
                enabled=False,
                policy=AIPolicyConfig(
                    enabled=True, enforcement_level="mandatory", risk_threshold="high"
                ),
            )
        msg = str(exc.value)
        assert "ai_summary.enabled: true" in msg
        assert "ai_summary.policy.enabled: false" in msg
        assert "advisory" in msg

    def test_advisory_with_summaries_off_is_left_alone(self):
        """Deliberately narrow. Advisory records and never blocks, so this is
        inert rather than dangerous — and refusing it would break deployments
        that are working today."""
        cfg = AISummaryConfig(
            enabled=False,
            policy=AIPolicyConfig(
                enabled=True, enforcement_level="advisory", risk_threshold="high"
            ),
        )
        assert cfg.policy.enforcement_level == "advisory"

    def test_a_mandatory_gate_with_summaries_on_is_the_supported_case(self):
        cfg = AISummaryConfig(
            enabled=True,
            policy=AIPolicyConfig(
                enabled=True, enforcement_level="mandatory", risk_threshold="high"
            ),
        )
        assert cfg.enabled and cfg.policy.enforcement_level == "mandatory"


# ── 3. releasing a run held because nothing ever ruled on it ─────────


class TestOverrideReleasesARunThatWasNeverRuledOn:
    """`override` returned None when no evaluation existed and the endpoint
    turned that into a 409 — refusing in exactly the state where release is
    most needed, and advising the operator to wait for a verdict that was
    never coming."""

    async def _override(self, existing):
        db = AsyncMock()
        db.flush = AsyncMock()
        with (
            patch.object(ai_policy_service, "get_evaluation", new=AsyncMock(return_value=existing)),
            patch.object(
                ai_policy_service,
                "record_evaluation",
                new=AsyncMock(return_value=MagicMock(outcome="overridden", overridden_by=None)),
            ) as record,
        ):
            row = await ai_policy_service.override(
                db, run_id=uuid.uuid4(), actor="alice@terrapod", enforcement_level="mandatory"
            )
        return row, record

    async def test_a_hold_with_no_evaluation_is_released(self):
        row, _ = await self._override(None)
        assert row is not None
        assert row.overridden_by == "alice@terrapod"

    async def test_the_row_it_writes_is_honest_about_never_having_ruled(self):
        """Not a forged pass. An auditor must be able to tell this apart from
        a gate that actually ran and was overruled."""
        _, record = await self._override(None)
        kwargs = record.await_args.kwargs
        assert kwargs["outcome"] == "overridden"
        assert "never ruled" in kwargs["error"]
        assert kwargs.get("verdict") is None
        assert kwargs["enforcement_level"] == "mandatory"

    async def test_an_existing_verdict_is_stamped_not_replaced(self):
        existing = MagicMock(outcome="denied", overridden_by=None)
        row, record = await self._override(existing)
        assert row is existing
        assert row.overridden_by == "alice@terrapod"
        # The real verdict must survive: overriding a deny records who
        # overruled it, it does not erase what was decided.
        assert row.outcome == "denied"
        record.assert_not_awaited()
