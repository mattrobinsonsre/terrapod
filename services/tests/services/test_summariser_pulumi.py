"""The AI summary reads a Pulumi preview as a preview (#1569).

The issue says Pulumi previews get no AI summary because it lives in the
Terraform plan phase. They already got one: a preview digest is uploaded to the
same plan-JSON key, so the trigger fires and the summariser runs — the real runs
on a live stack have `plan_summary` rows.

What was wrong is subtler and worse than absence. The digest was handed to the
model labelled `PLAN_JSON`, so it was asked to read a Terraform plan that was
not there: to find `resource_changes` in a document whose changes live under
`steps`, and to answer about plans and applies on a workspace whose own UI says
previews and updates.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.summariser_prompt import render_prompt

pytestmark = pytest.mark.asyncio

MOD = "terrapod.services.summariser"


def _prompt(label: str) -> str:
    system, _user = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label=label,
        primary_input_lang="json",
        code_context_truncated="",
    )
    return system


class TestThePromptSpeaksPulumi:
    def test_a_pulumi_preview_gets_pulumis_vocabulary(self) -> None:
        out = _prompt("PULUMI_PREVIEW")
        assert "PULUMI PREVIEW" in out
        assert "URN" in out
        assert "`same` means unchanged" in out

    def test_a_terraform_plan_is_byte_identical_to_before(self) -> None:
        """Appended, never folded into the skill prompt: the prompt was tuned
        against Terraform plan JSON and must go on doing exactly that."""
        assert "PULUMI PREVIEW" not in _prompt("PLAN_JSON")

    def test_it_says_not_to_hunt_for_terraform_keys(self) -> None:
        """Reporting the absence of `resource_changes` as a finding is the
        failure mode of reading this document as a plan."""
        out = _prompt("PULUMI_PREVIEW")
        assert "resource_changes" in out
        assert "do not report their absence as a finding" in out

    def test_it_explains_the_secret_placeholder(self) -> None:
        """Pulumi's engine writes `[secret]` before Terrapod sees the log, so a
        model told nothing would report a redaction as a missing value."""
        assert "[secret]" in _prompt("PULUMI_PREVIEW")


class TestTheDigestIsNotPutThroughTerraformsPasses:
    """`_clean_plan_json_bytes`, `marked_values` and `redact_plan_json` all look
    for `prior_state`, `sensitive_values` and change blocks. A preview digest has
    none of them, so each would search a shape that is not there."""

    async def _gather(self, engine: str, raw: bytes):
        from terrapod.services import summariser

        run = MagicMock()
        run.id = "r1"
        run.workspace_id = "w1"
        run.configuration_version_id = None
        db = AsyncMock()
        # The workspace is passed in by the caller, which already holds it —
        # `_gather_inputs` does not look it up.
        ws = MagicMock(engine=engine)
        storage = MagicMock()
        storage.get = AsyncMock(return_value=raw)
        with (
            patch(f"{MOD}.get_storage", return_value=storage),
            patch(f"{MOD}._clean_plan_json_bytes", MagicMock(return_value=raw)) as clean,
            patch(f"{MOD}.marked_values", MagicMock(return_value=[])) as marked,
            patch(f"{MOD}.redact_plan_json", MagicMock(return_value=raw)) as redact,
            patch(f"{MOD}._sensitive_literals", AsyncMock(return_value=[])),
        ):
            out = await summariser._gather_inputs(db, run, "plan_summary", ws)
        return out, clean, marked, redact

    async def test_a_pulumi_run_skips_them_and_is_labelled_a_preview(self) -> None:
        digest = b'{"engine":"pulumi","change_summary":{"same":1},"steps":[],"has_changes":false}'
        (primary, label, lang, _ctx, _diff), clean, marked, redact = await self._gather(
            "pulumi", digest
        )
        assert label == "PULUMI_PREVIEW"
        assert lang == "json"
        clean.assert_not_called()
        marked.assert_not_called()
        redact.assert_not_called()

    async def test_a_terraform_run_still_goes_through_all_three(self) -> None:
        plan = b'{"resource_changes":[]}'
        (_primary, label, _lang, _ctx, _diff), clean, marked, redact = await self._gather(
            "terraform", plan
        )
        assert label == "PLAN_JSON"
        clean.assert_called_once()
        marked.assert_called_once()
        redact.assert_called_once()


class TestRedactionStillCoversEveryInput:
    """GHSA-5mpc-79pv-6mq7: this function is the one place everything bound for
    the model is assembled, so it is the one place redaction can cover all of it.

    The first version of the Pulumi branch took an early `return`, which skipped
    the shared tail that redacts the code context and diff against the
    workspace's own sensitive variable values — the exact guarantee the advisory
    exists to enforce. Pinned so it cannot come back.
    """

    def test_the_pulumi_branch_does_not_return_early(self) -> None:
        import inspect

        from terrapod.services import summariser

        src = inspect.getsource(summariser._gather_inputs)
        head, _, tail = src.partition('primary_label = "PULUMI_PREVIEW"')
        assert tail, "the Pulumi branch has moved; re-check this guard"
        # Everything between the branch and the shared redaction must not return.
        before_shared, _, _ = tail.partition("_sensitive_literals")
        assert "return primary" not in before_shared
