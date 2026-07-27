"""AI architecture critic service (#1036 Part 2 / #963).

Covers the new logic vs the validated spike: the state COMPACTION (secret
exclusion via the curated allowlist, ``instances`` fan-out, edge filtering,
truncation), the prompt render (grounding sections present/absent), result
coercion, and the trigger handler's disabled/invalid-payload no-ops.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import architecture_critic_service as svc
from terrapod.services.summariser_prompt import render_architecture_prompt


def _state(resources):
    return {"version": 4, "resources": resources}


class TestCompaction:
    def test_managed_resource_curated_attrs_and_instances(self):
        state = _state(
            [
                {
                    "mode": "managed",
                    "type": "aws_db_instance",
                    "name": "main",
                    "instances": [
                        {
                            "attributes": {
                                "multi_az": False,
                                "instance_class": "db.r5.2xlarge",
                                "backup_retention_period": 0,
                                # secret-bearing / irrelevant — MUST be dropped
                                "password": "s3cr3t",
                                "master_password": "hunter2",
                                "tags": {"env": "prod"},
                            }
                        }
                    ],
                }
            ]
        )
        resources, edges, truncated = svc.compact_state_for_critique(state)
        assert truncated is False
        assert len(resources) == 1
        r = resources[0]
        assert r["address"] == "aws_db_instance.main"
        assert r["instances"] == 1
        # Curated allowlist kept the architecture attrs...
        assert r["attrs"]["multi_az"] is False
        assert r["attrs"]["instance_class"] == "db.r5.2xlarge"
        assert r["attrs"]["backup_retention_period"] == 0
        # ...and dropped secrets + nested blocks entirely.
        assert "password" not in r["attrs"]
        assert "master_password" not in r["attrs"]
        assert "tags" not in r["attrs"]

    def test_count_fanout_recorded_as_instances(self):
        # A count/for_each resource is ONE address with instances=N — the signal
        # the critic must read as redundancy rather than a false SPOF.
        state = _state(
            [
                {
                    "mode": "managed",
                    "type": "aws_nat_gateway",
                    "name": "nat",
                    "instances": [
                        {"attributes": {"connectivity_type": "public"}},
                        {"attributes": {"connectivity_type": "public"}},
                        {"attributes": {"connectivity_type": "public"}},
                    ],
                }
            ]
        )
        resources, _, _ = svc.compact_state_for_critique(state)
        assert resources[0]["instances"] == 3

    def test_data_sources_dropped(self):
        state = _state(
            [
                {
                    "mode": "data",
                    "type": "aws_ami",
                    "name": "ubuntu",
                    "instances": [{"attributes": {}}],
                },
                {
                    "mode": "managed",
                    "type": "aws_instance",
                    "name": "web",
                    "instances": [{"attributes": {}}],
                },
            ]
        )
        resources, _, _ = svc.compact_state_for_critique(state)
        addrs = {r["address"] for r in resources}
        assert addrs == {"aws_instance.web"}

    def test_edges_from_dependencies_filtered_to_kept_nodes(self):
        state = _state(
            [
                {
                    "mode": "managed",
                    "type": "aws_instance",
                    "name": "app",
                    "instances": [{"attributes": {}, "dependencies": ["aws_security_group.web"]}],
                },
                {
                    "mode": "managed",
                    "type": "aws_security_group",
                    "name": "web",
                    "instances": [{"attributes": {}}],
                },
            ]
        )
        resources, edges, _ = svc.compact_state_for_critique(state)
        assert {
            "source": "aws_instance.app",
            "target": "aws_security_group.web",
            "kind": "depends-on",
        } in edges

    def test_truncation_flagged(self):
        many = [
            {
                "mode": "managed",
                "type": "null_resource",
                "name": f"n{i}",
                "instances": [{"attributes": {}}],
            }
            for i in range(svc.MAX_CRITIQUE_RESOURCES + 5)
        ]
        resources, _, truncated = svc.compact_state_for_critique(_state(many))
        assert truncated is True
        assert len(resources) == svc.MAX_CRITIQUE_RESOURCES

    def test_long_string_and_nonscalar_values_dropped(self):
        state = _state(
            [
                {
                    "mode": "managed",
                    "type": "aws_instance",
                    "name": "web",
                    "instances": [
                        {
                            "attributes": {
                                "instance_type": "t3.medium",  # kept
                                "cidr_block": "x" * 500,  # allowlisted key but too long → dropped
                            }
                        }
                    ],
                }
            ]
        )
        r = svc.compact_state_for_critique(state)[0][0]
        assert r["attrs"]["instance_type"] == "t3.medium"
        assert "cidr_block" not in r["attrs"]


class TestPromptRender:
    def test_grounding_sections_present_when_supplied(self):
        sysm, userm = render_architecture_prompt(
            resources_json='[{"address":"aws_db_instance.main"}]',
            edges_json="[]",
            security_findings='{"findings":[{"rule_id":"CKV_AWS_24"}]}',
            cost_estimate='{"monthly_total":100}',
        )
        assert "architect" in sysm.lower()
        assert "instances=N" in sysm  # the count/for_each rule
        assert "SECURITY_FINDINGS" in userm
        assert "COST_ESTIMATE" in userm
        assert "submit_architecture_critique" in userm

    def test_grounding_sections_omitted_when_absent(self):
        _, userm = render_architecture_prompt(resources_json="[]", edges_json="[]")
        assert "SECURITY_FINDINGS" not in userm
        assert "COST_ESTIMATE" not in userm
        assert "RESOURCES" in userm


class TestCoerceResult:
    def test_coerces_and_defaults(self):
        arch, risk, findings, deferred = svc._coerce_result(
            {
                "architecture": {"summary": "3-tier"},
                "risk_level": "high",
                "findings": [{"severity": "high", "title": "x"}],
                "deferred": ["encryption not visible"],
            }
        )
        assert arch == {"summary": "3-tier"}
        assert risk == "high"
        assert len(findings) == 1
        assert deferred == ["encryption not visible"]

    def test_missing_fields_default_safely(self):
        arch, risk, findings, deferred = svc._coerce_result({})
        assert arch == {}
        assert risk == ""
        assert findings == []
        assert deferred == []


class TestHandlerGating:
    @pytest.mark.asyncio
    async def test_disabled_no_ops(self, monkeypatch):
        monkeypatch.setattr(svc.settings.ai_architecture, "enabled", False)
        # Must not touch the DB / model when disabled.
        with patch.object(svc, "generate_critique", new=AsyncMock()) as gen:
            await svc.handle_architecture_critique({"workspace_id": "ws-1"})
            gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_payload_no_ops(self, monkeypatch):
        monkeypatch.setattr(svc.settings.ai_architecture, "enabled", True)
        with patch.object(svc, "generate_critique", new=AsyncMock()) as gen:
            await svc.handle_architecture_critique({"not_workspace": "x"})
            gen.assert_not_called()

    @pytest.mark.asyncio
    async def test_ws_prefixed_id_parses(self, monkeypatch):
        """Regression: the regenerate endpoint enqueues the ``ws-``-prefixed path
        param verbatim. The handler must strip the prefix — ``uuid.UUID('ws-…')``
        raises, so without stripping a normal regenerate would silently no-op."""
        import uuid

        monkeypatch.setattr(svc.settings.ai_architecture, "enabled", True)
        wid = uuid.uuid4()
        with patch.object(svc, "generate_critique", new=AsyncMock()) as gen:
            await svc.handle_architecture_critique({"workspace_id": f"ws-{wid}", "force": True})
            gen.assert_awaited_once_with(wid, force=True)

    @pytest.mark.asyncio
    async def test_bare_uuid_id_parses(self, monkeypatch):
        """The state-upload auto-hook enqueues the bare UUID; it must parse too."""
        import uuid

        monkeypatch.setattr(svc.settings.ai_architecture, "enabled", True)
        wid = uuid.uuid4()
        with patch.object(svc, "generate_critique", new=AsyncMock()) as gen:
            await svc.handle_architecture_critique({"workspace_id": str(wid), "force": False})
            gen.assert_awaited_once_with(wid, force=False)


class TestPostCritiqueFollowup:
    """Chat gating for post_critique_followup (#1036) — every guard maps to a
    Followup* the router turns into 4xx, plus the happy + model-failure paths."""

    async def test_disabled_feature_raises(self):
        with patch.object(svc.settings.ai_architecture, "enabled", False):
            with pytest.raises(svc.FollowupDisabled):
                await svc.post_critique_followup(AsyncMock(), uuid.uuid4(), "hi")

    async def test_zero_message_cap_raises(self):
        with (
            patch.object(svc.settings.ai_architecture, "enabled", True),
            patch.object(svc.settings.ai_architecture, "followup_max_messages", 0),
        ):
            with pytest.raises(svc.FollowupDisabled):
                await svc.post_critique_followup(AsyncMock(), uuid.uuid4(), "hi")

    async def test_no_ready_critique_raises(self):
        with (
            patch.object(svc.settings.ai_architecture, "enabled", True),
            patch.object(svc.settings.ai_architecture, "followup_max_messages", 20),
            patch.object(svc, "current_critique_for_workspace", AsyncMock(return_value=None)),
        ):
            with pytest.raises(svc.FollowupDisabled):
                await svc.post_critique_followup(AsyncMock(), uuid.uuid4(), "hi")

    async def test_non_ready_critique_raises(self):
        crit = SimpleNamespace(id=uuid.uuid4(), status="pending")
        with (
            patch.object(svc.settings.ai_architecture, "enabled", True),
            patch.object(svc.settings.ai_architecture, "followup_max_messages", 20),
            patch.object(svc, "current_critique_for_workspace", AsyncMock(return_value=crit)),
        ):
            with pytest.raises(svc.FollowupDisabled):
                await svc.post_critique_followup(AsyncMock(), uuid.uuid4(), "hi")

    async def test_cap_reached_raises(self):
        crit = SimpleNamespace(id=uuid.uuid4(), status="ready")
        prior = [
            SimpleNamespace(role="user", content="q1"),
            SimpleNamespace(role="assistant", content="a1"),
            SimpleNamespace(role="user", content="q2"),
        ]
        with (
            patch.object(svc.settings.ai_architecture, "enabled", True),
            patch.object(svc.settings.ai_architecture, "followup_max_messages", 2),
            patch.object(svc, "current_critique_for_workspace", AsyncMock(return_value=crit)),
            patch.object(svc, "list_critique_messages", AsyncMock(return_value=prior)),
        ):
            with pytest.raises(svc.FollowupCapReached):
                await svc.post_critique_followup(AsyncMock(), uuid.uuid4(), "hi")

    async def test_budget_exhausted_raises(self):
        crit = SimpleNamespace(id=uuid.uuid4(), status="ready")
        with (
            patch.object(svc.settings.ai_architecture, "enabled", True),
            patch.object(svc.settings.ai_architecture, "followup_max_messages", 20),
            patch.object(svc, "current_critique_for_workspace", AsyncMock(return_value=crit)),
            patch.object(svc, "list_critique_messages", AsyncMock(return_value=[])),
            patch.object(svc, "_budget_remaining", AsyncMock(return_value=0)),
        ):
            with pytest.raises(svc.FollowupBudgetExhausted):
                await svc.post_critique_followup(AsyncMock(), uuid.uuid4(), "hi")

    async def test_happy_path_persists_reply_and_emits(self):
        crit = SimpleNamespace(id=uuid.uuid4(), status="ready")
        db = AsyncMock()
        db.add = MagicMock()  # SQLAlchemy add is synchronous
        emit = AsyncMock()
        with (
            patch.object(svc.settings.ai_architecture, "enabled", True),
            patch.object(svc.settings.ai_architecture, "followup_max_messages", 20),
            patch.object(svc, "current_critique_for_workspace", AsyncMock(return_value=crit)),
            patch.object(svc, "list_critique_messages", AsyncMock(return_value=[])),
            patch.object(svc, "_budget_remaining", AsyncMock(return_value=None)),
            patch.object(svc, "_critique_grounding", return_value="GROUNDING"),
            patch.object(svc, "_call_chat_model", AsyncMock(return_value=("the answer", 5, 10))),
            patch.object(svc, "_budget_charge", AsyncMock()) as charge,
            patch.object(svc, "_emit_event", emit),
        ):
            out = await svc.post_critique_followup(db, uuid.uuid4(), "how would I make it HA?")
        assert out.role == "assistant"
        assert out.content == "the answer"
        assert out.input_tokens == 5 and out.output_tokens == 10
        assert not out.error_message
        charge.assert_awaited_once_with(15)  # in + out charged together
        emit.assert_awaited()  # architecture_critique_message_posted

    async def test_model_failure_persists_error_message_without_raising(self):
        crit = SimpleNamespace(id=uuid.uuid4(), status="ready")
        with (
            patch.object(svc.settings.ai_architecture, "enabled", True),
            patch.object(svc.settings.ai_architecture, "followup_max_messages", 20),
            patch.object(svc, "current_critique_for_workspace", AsyncMock(return_value=crit)),
            patch.object(svc, "list_critique_messages", AsyncMock(return_value=[])),
            patch.object(svc, "_budget_remaining", AsyncMock(return_value=None)),
            patch.object(svc, "_critique_grounding", return_value="GROUNDING"),
            patch.object(
                svc, "_call_chat_model", AsyncMock(side_effect=RuntimeError("model boom"))
            ),
            patch.object(svc, "_budget_charge", AsyncMock()),
            patch.object(svc, "_emit_event", AsyncMock()),
        ):
            db = AsyncMock()
            db.add = MagicMock()
            out = await svc.post_critique_followup(db, uuid.uuid4(), "q")
        # The assistant row is still persisted, carrying the error for the UI.
        assert out.role == "assistant"
        assert "model boom" in out.error_message
        assert out.content == ""
