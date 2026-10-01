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


# ── 4. a gate ruling on a plan it was only partly shown ──────────────


class TestAMandatoryGateRefusesAReducedPlan:
    """`_fit_plan_json` reduces an over-cap plan to address+actions skeletons.
    The gate still reported a clean pass on the part it could see, so padding a
    plan past `ai_summary.plan_json_max_bytes` pushed the offending change out
    of the model's view and through a MANDATORY gate — and the size of the plan
    is something whoever authors the configuration controls.

    Treated as the existing "no verdict" case is: un-ruled. That holds the run
    and leaves an admin to override after reading the plan themselves, which is
    the same machinery `decide_outcome` already uses for silence.
    """

    @staticmethod
    def _ws(enforcement: str):
        return SimpleNamespace(
            id=uuid.uuid4(),
            ai_summary_policy_enforcement=enforcement,
            ai_summary_context="",
        )

    async def _settle(self, enforcement: str, *, incomplete: str | None):
        """Drive `_settle_ai_policy_gate` and return the recorded kwargs."""
        from terrapod.services import summariser

        run = SimpleNamespace(id=uuid.uuid4(), workspace_id=uuid.uuid4())
        ws = self._ws(enforcement)
        recorded = AsyncMock()
        db = AsyncMock()
        with (
            patch.object(ai_policy_service, "record_evaluation", recorded),
            patch.object(ai_policy_service, "gate_applies_to", lambda *_a: True),
            patch.object(ai_policy_service, "effective_enforcement", lambda *_a: enforcement),
            patch.object(summariser, "_redrive_after_gate", AsyncMock(), create=True),
            patch("terrapod.services.run_service.complete_plan", AsyncMock(), create=True),
        ):
            await summariser._settle_ai_policy_gate(
                db,
                run,
                ws,
                kind="plan_summary",
                verdict={"decision": "allow", "reason": "looks fine"},
                risk_level="low",
                evidence_incomplete=incomplete,
            )
        recorded.assert_awaited_once()
        return recorded.await_args.kwargs

    @pytest.mark.asyncio
    async def test_a_mandatory_gate_is_un_ruled_when_the_plan_was_reduced(self):
        from terrapod.services.summariser import INCOMPLETE_EVIDENCE_ERROR

        kw = await self._settle("mandatory", incomplete=INCOMPLETE_EVIDENCE_ERROR)
        assert kw["outcome"] == "errored", "a model allow on a partial plan was honoured"
        assert "plan_json_max_bytes" in kw["error"]

    @pytest.mark.asyncio
    async def test_the_model_s_opinion_is_still_recorded(self):
        """The human deciding whether to override needs to see what the model
        made of the part it did read. Erroring must not discard it."""
        from terrapod.services.summariser import INCOMPLETE_EVIDENCE_ERROR

        kw = await self._settle("mandatory", incomplete=INCOMPLETE_EVIDENCE_ERROR)
        assert kw["verdict"] == {"decision": "allow", "reason": "looks fine"}

    @pytest.mark.asyncio
    async def test_an_advisory_gate_keeps_its_verdict(self):
        """Deliberately narrow: advisory never blocks, so overwriting its
        verdict with an error would lose the opinion and protect nothing. The
        prompt has already told the model which parts it could not see."""
        from terrapod.services.summariser import INCOMPLETE_EVIDENCE_ERROR

        kw = await self._settle("advisory", incomplete=INCOMPLETE_EVIDENCE_ERROR)
        assert kw["outcome"] == "passed"

    @pytest.mark.asyncio
    async def test_a_complete_plan_rules_normally_under_mandatory(self):
        kw = await self._settle("mandatory", incomplete=None)
        assert kw["outcome"] == "passed"

    def test_the_detector_fires_on_both_reduction_keys(self):
        from terrapod.services.summariser import _plan_evidence_withheld

        assert _plan_evidence_withheld('{"_reduced_changes": 3}')
        assert _plan_evidence_withheld('{"_omitted_changes": 1}')

    def test_the_detector_is_silent_on_a_whole_plan(self):
        """The fitter returns an over-cap plan byte-identical when it fits, so
        an ordinary plan must not be read as reduced."""
        from terrapod.services.summariser import _plan_evidence_withheld

        assert not _plan_evidence_withheld(
            '{"resource_changes": [{"address": "aws_s3_bucket.a", "change": {"actions": ["create"]}}]}'
        )

    def test_the_message_names_all_three_ways_out(self):
        from terrapod.services.summariser import INCOMPLETE_EVIDENCE_ERROR

        assert "override" in INCOMPLETE_EVIDENCE_ERROR
        assert "plan_json_max_bytes" in INCOMPLETE_EVIDENCE_ERROR
        assert "advisory" in INCOMPLETE_EVIDENCE_ERROR
