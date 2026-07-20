"""Tests for the AI cost-narrative enhancement (#871).

Covers the two things that matter most for this feature:

  * **Provenance** — advisories are always stamped ``source: "ai-estimate"``
    server-side and the model can never smuggle a computed figure through
    (``_normalise_advisories``).
  * **No state leakage** — the cost summariser reads ONLY the derived,
    non-sensitive ``cost_estimate.json`` artifact, never Terraform state or
    the plan JSON. Pinned at module-source level (hard-invariant tier) so a
    future "let the AI see the state too" change fails CI loudly.

Plus the gating + happy-path behaviour of the trigger handler.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import cost_summariser

# ---------------------------------------------------------------------------
# Provenance — the whole point of the data-first design
# ---------------------------------------------------------------------------


class TestNormaliseAdvisories:
    def test_stamps_ai_estimate_source_always(self):
        # Even if the model tries to claim a different source, we overwrite it.
        out = cost_summariser._normalise_advisories(
            [{"kind": "reserved", "title": "t", "detail": "d", "source": "computed"}]
        )
        assert len(out) == 1
        assert out[0]["source"] == "ai-estimate"

    def test_coerces_monthly_saving_range(self):
        out = cost_summariser._normalise_advisories(
            [
                {
                    "kind": "savings_plan",
                    "title": "t",
                    "detail": "d",
                    "monthly_saving_min": 10,
                    "monthly_saving_max": 25.5,
                }
            ]
        )
        assert out[0]["monthly_saving"] == {"min": 10.0, "max": 25.5}

    def test_missing_saving_is_none(self):
        out = cost_summariser._normalise_advisories([{"kind": "spot", "title": "t", "detail": "d"}])
        assert out[0]["monthly_saving"] is None

    def test_drops_invalid_kind_and_malformed(self):
        out = cost_summariser._normalise_advisories(
            [
                {"kind": "not_a_kind", "title": "t", "detail": "d"},  # bad kind
                {"kind": "rightsizing", "title": "t"},  # missing detail
                {"kind": "other", "title": "t", "detail": "d"},  # valid
                "garbage",  # not a dict
            ]
        )
        assert [a["kind"] for a in out] == ["other"]

    def test_non_list_returns_empty(self):
        assert cost_summariser._normalise_advisories(None) == []
        assert cost_summariser._normalise_advisories({"kind": "spot"}) == []

    def test_truncates_long_fields(self):
        out = cost_summariser._normalise_advisories(
            [{"kind": "other", "title": "x" * 500, "detail": "y" * 1000}]
        )
        assert len(out[0]["title"]) == 120
        assert len(out[0]["detail"]) == 600


class TestNormaliseEstimatedResources:
    def test_stamps_source_and_coerces_range(self):
        out = cost_summariser._normalise_estimated_resources(
            [
                {
                    "address": "google_compute_instance.x",
                    "type": "google_compute_instance",
                    "monthly_min": 12,
                    "monthly_max": 15.5,
                    "basis": "e2-medium us-central1",
                    "source": "computed",  # lie
                }
            ]
        )
        assert len(out) == 1
        assert out[0]["source"] == "ai-estimate"
        assert out[0]["monthly"] == {"min": 12.0, "max": 15.5}
        assert out[0]["address"] == "google_compute_instance.x"

    def test_drops_entries_without_address_or_numeric_range(self):
        out = cost_summariser._normalise_estimated_resources(
            [
                {"type": "x", "monthly_min": 1, "monthly_max": 2},  # no address
                {"address": "a.b", "monthly_min": "n/a", "monthly_max": 2},  # non-numeric
                {"address": "a.b", "monthly_min": 1, "monthly_max": 2, "basis": "ok"},  # valid
            ]
        )
        assert [e["address"] for e in out] == ["a.b"]

    def test_non_list_returns_empty(self):
        assert cost_summariser._normalise_estimated_resources(None) == []


# ---------------------------------------------------------------------------
# No state leakage — hard invariant (Code ↔ Tests contract)
# ---------------------------------------------------------------------------


class TestCostNoStateLeakage:
    """The cost narrative MUST read only the derived cost estimate — never
    Terraform state or the plan JSON (both can carry sensitive values).

    Pinned at module-source level: any future change that pulls state or
    plan JSON into the cost prompt fails CI here.
    """

    @staticmethod
    def _source() -> str:
        import inspect

        return inspect.getsource(cost_summariser)

    def test_reads_only_the_cost_estimate_artifact(self):
        src = self._source()
        assert "cost_estimate_key" in src, "cost summariser must read the cost estimate artifact"

    def test_no_state_key_helpers(self):
        src = self._source()
        for helper in ("state_key", "state_index_key", "state_backup_key"):
            assert helper not in src, f"cost summariser must not reference {helper!r}"

    def test_no_state_version_or_plan_json_references(self):
        src = self._source()
        for name in (
            "StateVersion",
            "state_versions",
            "state_version_id",
            "json_output_key",  # the plan-JSON artifact key — not for cost
            "before_sensitive",
            "after_sensitive",
        ):
            assert name not in src, f"cost summariser must not reference {name!r}"


# ---------------------------------------------------------------------------
# Handler gating + happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_globally_is_noop():
    with patch.object(cost_summariser.settings.ai_summary, "enabled", False):
        with patch("terrapod.services.cost_summariser.get_db_session") as sess:
            await cost_summariser.handle_ai_cost_summary({"run_id": str(uuid.uuid4())})
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
async def test_ready_path_stamps_advisories_and_emits(monkeypatch):
    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), has_cost_estimate=True)
    ws = MagicMock(id=uuid.uuid4(), ai_summary_context="")
    db = _db_returning(run, ws)

    storage = MagicMock()
    storage.get = AsyncMock(return_value=b'{"currency":"USD","total":{"min":73,"max":73}}')
    upsert = AsyncMock()
    emit = AsyncMock()
    call_model = AsyncMock(
        return_value=(
            {
                "estimated_resources": [
                    {
                        "address": "azurerm_storage_account.a",
                        "type": "azurerm_storage_account",
                        "monthly_min": 5,
                        "monthly_max": 8,
                        "basis": "LRS hot ~100GB",
                        "source": "computed",  # model lie — must be overwritten
                    },
                ],
                "narrative": "Roughly $73/mo, driven by aws_instance.web.",
                "advisories": [
                    {
                        "kind": "reserved",
                        "title": "RI the box",
                        "detail": "d",
                        "monthly_saving_min": 20,
                        "monthly_saving_max": 30,
                    },
                ],
            },
            120,
            60,
        )
    )

    monkeypatch.setattr(cost_summariser.settings.ai_summary, "enabled", True)
    with (
        patch("terrapod.services.cost_summariser.get_db_session") as sess,
        patch("terrapod.services.cost_summariser._resolve_workspace_mode", return_value=True),
        patch("terrapod.services.cost_summariser._budget_remaining", AsyncMock(return_value=None)),
        patch("terrapod.services.cost_summariser._budget_charge", AsyncMock()),
        patch("terrapod.services.cost_summariser.get_storage", return_value=storage),
        patch("terrapod.services.cost_summariser._call_model", call_model),
        patch("terrapod.services.cost_summariser._upsert_cost_summary", upsert),
        patch("terrapod.services.cost_summariser._emit_summary_event", emit),
    ):
        sess.return_value.__aenter__ = AsyncMock(return_value=db)
        sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await cost_summariser.handle_ai_cost_summary({"run_id": str(run.id)})

    # The terminal upsert is status="ready" with the PRIMARY estimated
    # resources (provenance forced) + a stamped advisory.
    ready = [c for c in upsert.await_args_list if c.kwargs.get("status") == "ready"]
    assert ready, "expected a ready upsert"
    kw = ready[-1].kwargs
    er = kw["estimated_resources"][0]
    assert er["address"] == "azurerm_storage_account.a"
    assert er["monthly"] == {"min": 5.0, "max": 8.0}
    assert er["source"] == "ai-estimate"  # the model's "computed" was overwritten
    assert kw["narrative"].startswith("Roughly")
    assert kw["advisories"][0]["source"] == "ai-estimate"
    assert kw["advisories"][0]["monthly_saving"] == {"min": 20.0, "max": 30.0}
    # A ready SSE event fired.
    assert any(c.args and c.args[0] == "cost_summary_ready" for c in emit.await_args_list)


@pytest.mark.asyncio
async def test_workspace_mode_disabled_skips(monkeypatch):
    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), has_cost_estimate=True)
    ws = MagicMock(id=uuid.uuid4(), ai_summary_context="")
    db = _db_returning(run, ws)
    upsert = AsyncMock()
    emit = AsyncMock()
    call_model = AsyncMock()

    monkeypatch.setattr(cost_summariser.settings.ai_summary, "enabled", True)
    with (
        patch("terrapod.services.cost_summariser.get_db_session") as sess,
        patch("terrapod.services.cost_summariser._resolve_workspace_mode", return_value=False),
        patch("terrapod.services.cost_summariser._call_model", call_model),
        patch("terrapod.services.cost_summariser._upsert_cost_summary", upsert),
        patch("terrapod.services.cost_summariser._emit_summary_event", emit),
    ):
        sess.return_value.__aenter__ = AsyncMock(return_value=db)
        sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await cost_summariser.handle_ai_cost_summary({"run_id": str(run.id)})

    call_model.assert_not_awaited()  # never calls the model when workspace is off
    assert [c.kwargs.get("status") for c in upsert.await_args_list] == ["skipped"]
    assert any(c.args and c.args[0] == "cost_summary_skipped" for c in emit.await_args_list)


@pytest.mark.asyncio
async def test_missing_artifact_skips(monkeypatch):
    from terrapod.storage.protocol import ObjectNotFoundError

    run = MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), has_cost_estimate=True)
    ws = MagicMock(id=uuid.uuid4(), ai_summary_context="")
    db = _db_returning(run, ws)
    storage = MagicMock()
    storage.get = AsyncMock(side_effect=ObjectNotFoundError("gone"))
    upsert = AsyncMock()
    emit = AsyncMock()
    call_model = AsyncMock()

    monkeypatch.setattr(cost_summariser.settings.ai_summary, "enabled", True)
    with (
        patch("terrapod.services.cost_summariser.get_db_session") as sess,
        patch("terrapod.services.cost_summariser._resolve_workspace_mode", return_value=True),
        patch("terrapod.services.cost_summariser._budget_remaining", AsyncMock(return_value=None)),
        patch("terrapod.services.cost_summariser.get_storage", return_value=storage),
        patch("terrapod.services.cost_summariser._call_model", call_model),
        patch("terrapod.services.cost_summariser._upsert_cost_summary", upsert),
        patch("terrapod.services.cost_summariser._emit_summary_event", emit),
    ):
        sess.return_value.__aenter__ = AsyncMock(return_value=db)
        sess.return_value.__aexit__ = AsyncMock(return_value=False)
        await cost_summariser.handle_ai_cost_summary({"run_id": str(run.id)})

    call_model.assert_not_awaited()
    assert upsert.await_args_list[-1].kwargs["status"] == "skipped"
