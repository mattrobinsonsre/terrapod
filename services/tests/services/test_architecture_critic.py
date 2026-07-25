"""Tests for the AI architecture-critique enhancement (#963/#1036).

Covers the two things that matter most for this feature:

  * **Findings normalisation** — the model's findings are coerced into the
    stored shape, invalid severity/category entries dropped, prose truncated.
  * **No state leakage** — the critic reads ONLY the run's plan JSON (cleaned +
    bounded by the plan-summary path's helpers), never Terraform state. Pinned
    at module-source level (hard-invariant tier) so a future "let the AI see the
    state too" change fails CI loudly.

Plus the gating + happy-path behaviour of the trigger handler. Near-exact clone
of test_cost_summariser.py.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import architecture_critic

# ---------------------------------------------------------------------------
# Findings + risk-level normalisation
# ---------------------------------------------------------------------------


class TestNormaliseFindings:
    def test_keeps_valid_finding(self):
        out = architecture_critic._normalise_findings(
            [
                {
                    "severity": "high",
                    "category": "reliability",
                    "title": "Single-AZ DB",
                    "detail": "no multi_az",
                    "address": "aws_db_instance.main",
                }
            ]
        )
        assert len(out) == 1
        assert out[0]["category"] == "reliability"
        assert out[0]["address"] == "aws_db_instance.main"

    def test_drops_invalid_severity_and_category(self):
        out = architecture_critic._normalise_findings(
            [
                {"severity": "boom", "category": "security", "title": "t", "detail": "d"},
                {"severity": "high", "category": "nope", "title": "t", "detail": "d"},
                {"severity": "low", "category": "cost", "title": "t", "detail": "d"},  # valid
                "garbage",
            ]
        )
        assert [f["category"] for f in out] == ["cost"]

    def test_drops_missing_title_or_detail(self):
        out = architecture_critic._normalise_findings(
            [
                {"severity": "info", "category": "operations", "title": "t"},  # no detail
                {"severity": "info", "category": "operations", "detail": "d"},  # no title
            ]
        )
        assert out == []

    def test_address_optional_defaults_empty(self):
        out = architecture_critic._normalise_findings(
            [{"severity": "medium", "category": "scalability", "title": "t", "detail": "d"}]
        )
        assert out[0]["address"] == ""

    def test_truncates_long_fields(self):
        out = architecture_critic._normalise_findings(
            [{"severity": "low", "category": "other", "title": "x" * 500, "detail": "y" * 1000}]
        )
        assert len(out[0]["title"]) == 120
        assert len(out[0]["detail"]) == 600

    def test_non_list_returns_empty(self):
        assert architecture_critic._normalise_findings(None) == []
        assert architecture_critic._normalise_findings({"severity": "low"}) == []


class TestNormaliseRiskLevel:
    def test_keeps_valid(self):
        for level in ("critical", "high", "medium", "low", "none"):
            assert architecture_critic._normalise_risk_level(level) == level

    def test_defaults_none_for_invalid(self):
        assert architecture_critic._normalise_risk_level("scary") == "none"
        assert architecture_critic._normalise_risk_level(None) == "none"


# ---------------------------------------------------------------------------
# No state leakage — hard invariant (Code ↔ Tests contract)
# ---------------------------------------------------------------------------


class TestArchitectureCritiqueNoStateLeakage:
    """The critique MUST read only the run's plan JSON, cleaned + bounded by the
    plan-summary path's helpers — never Terraform state (which carries sensitive
    values).

    Pinned at module-source level: any future change that pulls state into the
    critique prompt fails CI here.
    """

    @staticmethod
    def _source() -> str:
        import inspect

        return inspect.getsource(architecture_critic)

    def test_reads_plan_json_via_shared_helpers(self):
        src = self._source()
        assert "plan_json_output_key" in src, "critic must read the plan JSON artifact"
        # Reuse the plan-summary path's redaction — no new redaction here.
        assert "_clean_plan_json_bytes" in src, "critic must clean plan JSON before the model"
        assert "_fit_plan_json" in src, "critic must bound plan JSON before the model"

    def test_no_state_key_helpers(self):
        src = self._source()
        for helper in ("state_key", "state_index_key", "state_backup_key"):
            assert helper not in src, f"critic must not reference {helper!r}"

    def test_no_state_version_references(self):
        src = self._source()
        for name in ("StateVersion", "state_versions", "state_version_id"):
            assert name not in src, f"critic must not reference {name!r}"


# ---------------------------------------------------------------------------
# Handler gating + happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_globally_is_noop():
    with patch.object(architecture_critic.settings.ai_summary, "enabled", False):
        with patch("terrapod.services.architecture_critic.get_db_session") as sess:
            await architecture_critic.handle_ai_architecture_critique({"run_id": str(uuid.uuid4())})
            sess.assert_not_called()


def _db_returning(run, ws):
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=ws)),
        ]
    )
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_ready_path_normalises_and_emits(monkeypatch):
    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), has_json_output=True)
    ws = MagicMock(id=uuid.uuid4(), ai_summary_context="")
    db = _db_returning(run, ws)

    storage = MagicMock()
    storage.get = AsyncMock(return_value=b'{"planned_values":{"root_module":{}}}')
    upsert = AsyncMock()
    emit = AsyncMock()
    call_model = AsyncMock(
        return_value=(
            {
                "critique": "The proposed VPC is single-AZ; the DB has no replica.",
                "risk_level": "high",
                "findings": [
                    {
                        "severity": "high",
                        "category": "reliability",
                        "title": "Single-AZ database",
                        "detail": "aws_db_instance.main has no multi_az.",
                        "address": "aws_db_instance.main",
                    },
                    {
                        "severity": "bogus",  # dropped
                        "category": "security",
                        "title": "x",
                        "detail": "y",
                    },
                ],
            },
            120,
            60,
        )
    )

    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", True)
    with (
        patch("terrapod.services.architecture_critic.get_db_session") as sess,
        patch("terrapod.services.architecture_critic._resolve_workspace_mode", return_value=True),
        patch(
            "terrapod.services.architecture_critic._budget_remaining",
            AsyncMock(return_value=None),
        ),
        patch("terrapod.services.architecture_critic._budget_charge", AsyncMock()),
        patch("terrapod.services.architecture_critic.get_storage", return_value=storage),
        patch("terrapod.services.architecture_critic._call_model", call_model),
        patch("terrapod.services.architecture_critic._upsert_architecture_critique", upsert),
        patch("terrapod.services.architecture_critic._emit_summary_event", emit),
    ):
        sess.return_value.__aenter__ = AsyncMock(return_value=db)
        sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await architecture_critic.handle_ai_architecture_critique({"run_id": str(run.id)})

    ready = [c for c in upsert.await_args_list if c.kwargs.get("status") == "ready"]
    assert ready, "expected a ready upsert"
    kw = ready[-1].kwargs
    assert kw["risk_level"] == "high"
    assert kw["critique"].startswith("The proposed")
    # Only the valid finding survived normalisation.
    assert [f["category"] for f in kw["findings"]] == ["reliability"]
    assert any(c.args and c.args[0] == "architecture_critique_ready" for c in emit.await_args_list)


@pytest.mark.asyncio
async def test_workspace_mode_disabled_skips(monkeypatch):
    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), has_json_output=True)
    ws = MagicMock(id=uuid.uuid4(), ai_summary_context="")
    db = _db_returning(run, ws)
    upsert = AsyncMock()
    emit = AsyncMock()
    call_model = AsyncMock()

    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", True)
    with (
        patch("terrapod.services.architecture_critic.get_db_session") as sess,
        patch("terrapod.services.architecture_critic._resolve_workspace_mode", return_value=False),
        patch("terrapod.services.architecture_critic._call_model", call_model),
        patch("terrapod.services.architecture_critic._upsert_architecture_critique", upsert),
        patch("terrapod.services.architecture_critic._emit_summary_event", emit),
    ):
        sess.return_value.__aenter__ = AsyncMock(return_value=db)
        sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await architecture_critic.handle_ai_architecture_critique({"run_id": str(run.id)})

    call_model.assert_not_awaited()  # never calls the model when workspace is off
    assert [c.kwargs.get("status") for c in upsert.await_args_list] == ["skipped"]
    assert any(
        c.args and c.args[0] == "architecture_critique_skipped" for c in emit.await_args_list
    )


@pytest.mark.asyncio
async def test_missing_plan_json_skips(monkeypatch):
    from terrapod.storage.protocol import ObjectNotFoundError

    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), has_json_output=True)
    ws = MagicMock(id=uuid.uuid4(), ai_summary_context="")
    db = _db_returning(run, ws)
    storage = MagicMock()
    storage.get = AsyncMock(side_effect=ObjectNotFoundError("gone"))
    upsert = AsyncMock()
    emit = AsyncMock()
    call_model = AsyncMock()

    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", True)
    with (
        patch("terrapod.services.architecture_critic.get_db_session") as sess,
        patch("terrapod.services.architecture_critic._resolve_workspace_mode", return_value=True),
        patch(
            "terrapod.services.architecture_critic._budget_remaining",
            AsyncMock(return_value=None),
        ),
        patch("terrapod.services.architecture_critic.get_storage", return_value=storage),
        patch("terrapod.services.architecture_critic._call_model", call_model),
        patch("terrapod.services.architecture_critic._upsert_architecture_critique", upsert),
        patch("terrapod.services.architecture_critic._emit_summary_event", emit),
    ):
        sess.return_value.__aenter__ = AsyncMock(return_value=db)
        sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await architecture_critic.handle_ai_architecture_critique({"run_id": str(run.id)})

    call_model.assert_not_awaited()
    assert upsert.await_args_list[-1].kwargs["status"] == "skipped"


@pytest.mark.asyncio
async def test_no_plan_json_run_is_noop(monkeypatch):
    # A run that never produced plan JSON is skipped before any upsert/model.
    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), has_json_output=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[MagicMock(scalar_one_or_none=MagicMock(return_value=run))])
    upsert = AsyncMock()

    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", True)
    with (
        patch("terrapod.services.architecture_critic.get_db_session") as sess,
        patch("terrapod.services.architecture_critic._upsert_architecture_critique", upsert),
    ):
        sess.return_value.__aenter__ = AsyncMock(return_value=db)
        sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await architecture_critic.handle_ai_architecture_critique({"run_id": str(run.id)})

    upsert.assert_not_awaited()


# ---------------------------------------------------------------------------
# Chat follow-ups — early gate behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_disabled_global_raises(monkeypatch):
    from terrapod.services.summariser import FollowupDisabled

    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", False)
    with pytest.raises(FollowupDisabled):
        await architecture_critic.post_architecture_followup(
            db=AsyncMock(),
            architecture_critique=MagicMock(),
            run=MagicMock(),
            workspace=MagicMock(),
            user_message_text="hi",
        )


@pytest.mark.asyncio
async def test_followup_workspace_off_raises(monkeypatch):
    from terrapod.services.summariser import FollowupDisabled

    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", True)
    monkeypatch.setattr(
        architecture_critic.settings.ai_summary, "followup_max_messages_per_run", 10
    )
    with patch("terrapod.services.architecture_critic._resolve_workspace_mode", return_value=False):
        with pytest.raises(FollowupDisabled):
            await architecture_critic.post_architecture_followup(
                db=AsyncMock(),
                architecture_critique=MagicMock(),
                run=MagicMock(),
                workspace=MagicMock(),
                user_message_text="hi",
            )


@pytest.mark.asyncio
async def test_followup_empty_text_raises(monkeypatch):
    from terrapod.services.summariser import FollowupError

    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", True)
    monkeypatch.setattr(
        architecture_critic.settings.ai_summary, "followup_max_messages_per_run", 10
    )
    with patch("terrapod.services.architecture_critic._resolve_workspace_mode", return_value=True):
        with pytest.raises(FollowupError):
            await architecture_critic.post_architecture_followup(
                db=AsyncMock(),
                architecture_critique=MagicMock(),
                run=MagicMock(),
                workspace=MagicMock(),
                user_message_text="   ",
            )


@pytest.mark.asyncio
async def test_followup_happy_path_persists_and_emits(monkeypatch):
    # Full turn: persists the assistant reply + telemetry and emits the
    # architecture_critique_message_posted SSE event (the SSE half of the contract).
    monkeypatch.setattr(architecture_critic.settings.ai_summary, "enabled", True)
    monkeypatch.setattr(
        architecture_critic.settings.ai_summary, "followup_max_messages_per_run", 10
    )

    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4())
    ws = MagicMock(id=uuid.uuid4())
    critique = MagicMock(id=uuid.uuid4(), critique="", risk_level="none", findings=[])

    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar=MagicMock(return_value=0)),  # user-turn count
            MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))),
        ]
    )
    storage = MagicMock()
    storage.get = AsyncMock(return_value=b'{"planned_values":{}}')
    emit = AsyncMock()

    with (
        patch("terrapod.services.architecture_critic._resolve_workspace_mode", return_value=True),
        patch(
            "terrapod.services.architecture_critic._budget_remaining",
            AsyncMock(return_value=None),
        ),
        patch("terrapod.services.architecture_critic._budget_charge", AsyncMock()),
        patch("terrapod.services.architecture_critic.get_storage", return_value=storage),
        patch(
            "terrapod.services.architecture_critic._call_chat_model",
            AsyncMock(return_value=("Add a multi-AZ replica.", 120, 30)),
        ),
        patch("terrapod.services.architecture_critic._emit_summary_event", emit),
    ):
        row = await architecture_critic.post_architecture_followup(
            db=db, architecture_critique=critique, run=run, workspace=ws, user_message_text="how?"
        )

    assert row.role == "assistant"
    assert row.content == "Add a multi-AZ replica."
    assert row.output_tokens == 30
    assert any(
        c.args and c.args[0] == "architecture_critique_message_posted" for c in emit.await_args_list
    )
