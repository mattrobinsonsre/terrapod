"""Unit tests for the AI plan summariser (#401)."""

import io
import tarfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import summariser

# ── Helpers ───────────────────────────────────────────────────────────────


def _mock_workspace(*, mode="default", ctx="", ws_id=None):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.ai_summary_mode = mode
    ws.ai_summary_context = ctx
    return ws


def _mock_run(*, ws_id=None, cv_id=None):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = ws_id or uuid.uuid4()
    run.configuration_version_id = cv_id
    return run


# ── _resolve_workspace_mode truth table ──────────────────────────────────


@pytest.mark.parametrize(
    "global_enabled,mode,expected",
    [
        (True, "default", True),
        (True, "enabled", True),
        (True, "disabled", False),
        (False, "default", False),
        (False, "enabled", False),  # global wins
        (False, "disabled", False),
    ],
)
def test_resolve_workspace_mode_truth_table(global_enabled, mode, expected):
    ws = _mock_workspace(mode=mode)
    with patch.object(summariser.settings.ai_summary, "enabled", global_enabled):
        assert summariser._resolve_workspace_mode(ws) is expected


# ── Truncation ───────────────────────────────────────────────────────────


def test_truncate_head_preserves_head():
    data = b"a" * 100 + b"TAIL"
    out = summariser._truncate_head(data, 50)
    assert out.startswith("a" * 50)
    assert "TAIL" not in out
    assert "truncated from tail" in out


def test_truncate_head_no_op_when_under_cap():
    data = b"small"
    assert summariser._truncate_head(data, 100) == "small"


def test_truncate_tail_preserves_tail():
    data = b"HEAD" + b"a" * 100
    out = summariser._truncate_tail(data, 50)
    assert out.endswith("a" * 50)
    assert "HEAD" not in out
    assert "truncated from head" in out


def test_truncate_tail_no_op_when_under_cap():
    assert summariser._truncate_tail(b"small", 100) == "small"


def test_truncate_zero_cap_returns_full_string():
    # 0 means "unlimited" in this helper — _gather_inputs uses the
    # config value directly. Code-context max=0 disables code entirely,
    # but the truncation primitives themselves are no-ops at 0.
    assert summariser._truncate_head(b"abc", 0) == "abc"


# ── _extract_tf_sources ──────────────────────────────────────────────────


def _build_tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_extract_tf_sources_includes_tf_files():
    tarball = _build_tarball(
        {
            "main.tf": b'resource "aws_vpc" "this" {}',
            "vars.tf": b'variable "name" {}',
            "README.md": b"not tf",  # must be skipped
        }
    )
    out = summariser._extract_tf_sources(tarball, 100_000)
    assert "main.tf" in out
    assert "vars.tf" in out
    assert "aws_vpc" in out
    assert "not tf" not in out


def test_extract_tf_sources_zero_cap_returns_empty():
    tarball = _build_tarball({"main.tf": b'resource "aws_vpc" "this" {}'})
    assert summariser._extract_tf_sources(tarball, 0) == ""


def test_extract_tf_sources_respects_byte_cap():
    tarball = _build_tarball(
        {
            "a.tf": b"x" * 500,
            "b.tf": b"y" * 500,
            "c.tf": b"z" * 500,
        }
    )
    out = summariser._extract_tf_sources(tarball, 600)
    # cap is hit after first file plus header (or after second small one)
    assert len(out) <= 1200  # generous bound including headers
    assert "x" in out


def test_extract_tf_sources_returns_empty_on_corrupt_tarball():
    assert summariser._extract_tf_sources(b"not a tarball", 100) == ""


# ── JSON parsing ─────────────────────────────────────────────────────────


def test_parse_clean_json():
    text = '{"description": "ok", "risk_level": "low", "risk_factors": []}'
    parsed = summariser._parse_model_json(text)
    assert parsed["risk_level"] == "low"


def test_parse_json_wrapped_in_code_fence():
    text = '```json\n{"description": "ok", "risk_level": "low", "risk_factors": []}\n```'
    parsed = summariser._parse_model_json(text)
    assert parsed["description"] == "ok"


def test_parse_json_with_leading_prose():
    text = 'Here is the summary:\n{"description": "x", "risk_level": "medium", "risk_factors": []}'
    parsed = summariser._parse_model_json(text)
    assert parsed["risk_level"] == "medium"


def test_parse_unparseable_raises():
    with pytest.raises(ValueError):
        summariser._parse_model_json("this is not json")


def test_parse_fenced_block_with_trailing_prose():
    """Opus sometimes emits ```json {...} ``` AND adds prose after the
    fence. The balanced-brace fallback alone would slurp the trailing
    text into the parse; the fence-aware path returns clean JSON.
    """
    text = '```json\n{"description": "ok", "risk_level": "low", "risk_factors": []}\n```\n\nLet me know if you want more detail.'
    parsed = summariser._parse_model_json(text)
    assert parsed["risk_level"] == "low"


def test_parse_fenced_block_without_json_tag():
    text = '```\n{"description": "ok", "risk_level": "low", "risk_factors": []}\n```'
    parsed = summariser._parse_model_json(text)
    assert parsed["description"] == "ok"


# ── truncation handling ─────────────────────────────────────────────────


async def test_call_model_raises_on_finish_length():
    """When Bedrock stops mid-JSON because max_output_tokens ran out we
    must call that out explicitly — not surface a misleading 'not JSON'.
    """
    truncated = '{"description": "this stops mid-way",'  # no closing brace
    fake_choice = MagicMock()
    fake_choice.message.content = truncated
    fake_choice.finish_reason = "length"
    fake_resp = MagicMock(
        choices=[fake_choice], usage=MagicMock(prompt_tokens=10, completion_tokens=1024)
    )

    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch(
            "terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=fake_resp)
        ),
    ):
        with pytest.raises(RuntimeError, match="truncated at max_output_tokens"):
            await summariser._call_model(
                system_message="s", user_message="u", max_output_tokens=1024
            )


async def test_call_model_parse_failure_includes_finish_reason():
    """Non-length parse failures should surface finish_reason + length
    in the error string so the operator can tell stop-vs-content
    failures apart in the UI / logs.
    """
    fake_choice = MagicMock()
    fake_choice.message.content = "I cannot summarise this plan."
    fake_choice.finish_reason = "stop"
    fake_resp = MagicMock(
        choices=[fake_choice], usage=MagicMock(prompt_tokens=10, completion_tokens=8)
    )

    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch(
            "terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=fake_resp)
        ),
    ):
        with pytest.raises(ValueError, match="finish_reason=stop"):
            await summariser._call_model(
                system_message="s", user_message="u", max_output_tokens=1024
            )


# ── _build_litellm_kwargs ───────────────────────────────────────────────


def test_build_litellm_kwargs_includes_aws_role_when_set():
    """aws_role_arn populates LiteLLM's aws_role_name + session + external_id."""
    with (
        patch.object(summariser.settings.ai_summary, "model", "bedrock/anthropic.claude-opus-4-8"),
        patch.object(summariser.settings.ai_summary, "api_base", ""),
        patch.object(summariser.settings.ai_summary.auth, "api_key", ""),
        patch.object(summariser.settings.ai_summary.auth, "aws_region", "us-east-1"),
        patch.object(summariser.settings.ai_summary.auth, "aws_role_arn", "arn:aws:iam::1:role/r"),
        patch.object(summariser.settings.ai_summary.auth, "aws_session_name", "tp-sess"),
        patch.object(summariser.settings.ai_summary.auth, "aws_external_id", "ext-id-123"),
    ):
        kw = summariser._build_litellm_kwargs(
            system_message="sys", user_message="usr", max_output_tokens=100
        )
        assert kw["model"] == "bedrock/anthropic.claude-opus-4-8"
        assert kw["aws_region_name"] == "us-east-1"
        assert kw["aws_role_name"] == "arn:aws:iam::1:role/r"
        assert kw["aws_session_name"] == "tp-sess"
        assert kw["aws_external_id"] == "ext-id-123"


def test_build_litellm_kwargs_omits_role_when_unset():
    """aws_role_arn empty → no aws_role_name kwarg (pod's ambient creds used directly)."""
    with (
        patch.object(summariser.settings.ai_summary, "model", "bedrock/anthropic.claude-opus-4-8"),
        patch.object(summariser.settings.ai_summary, "api_base", ""),
        patch.object(summariser.settings.ai_summary.auth, "api_key", ""),
        patch.object(summariser.settings.ai_summary.auth, "aws_region", "us-east-1"),
        patch.object(summariser.settings.ai_summary.auth, "aws_role_arn", ""),
        patch.object(summariser.settings.ai_summary.auth, "aws_session_name", "x"),
        patch.object(summariser.settings.ai_summary.auth, "aws_external_id", ""),
    ):
        kw = summariser._build_litellm_kwargs(
            system_message="sys", user_message="usr", max_output_tokens=100
        )
        assert "aws_role_name" not in kw
        assert "aws_external_id" not in kw


def test_build_litellm_kwargs_passes_api_key_and_base():
    """Bearer providers get api_key + api_base; AWS kwargs harmless when ignored."""
    with (
        patch.object(summariser.settings.ai_summary, "model", "openai/gpt-5"),
        patch.object(summariser.settings.ai_summary, "api_base", "https://vllm.local/v1"),
        patch.object(summariser.settings.ai_summary.auth, "api_key", "sk-abc"),
        patch.object(summariser.settings.ai_summary.auth, "aws_region", "us-east-1"),
        patch.object(summariser.settings.ai_summary.auth, "aws_role_arn", ""),
        patch.object(summariser.settings.ai_summary.auth, "aws_session_name", "x"),
        patch.object(summariser.settings.ai_summary.auth, "aws_external_id", ""),
    ):
        kw = summariser._build_litellm_kwargs(
            system_message="sys", user_message="usr", max_output_tokens=100
        )
        assert kw["api_key"] == "sk-abc"
        assert kw["api_base"] == "https://vllm.local/v1"


# ── handle_ai_plan_summary integration ──────────────────────────────────


async def test_handler_no_op_when_globally_disabled():
    with patch.object(summariser.settings.ai_summary, "enabled", False):
        # No DB session should be opened — the handler short-circuits
        with patch("terrapod.services.summariser.get_db_session") as mock_session:
            await summariser.handle_ai_plan_summary({"run_id": str(uuid.uuid4())})
            mock_session.assert_not_called()


async def test_handler_skips_when_workspace_disabled():
    run = _mock_run()
    ws = _mock_workspace(mode="disabled", ws_id=run.workspace_id)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=ws)),
        ]
    )
    mock_db.commit = AsyncMock()

    upsert = AsyncMock()
    with (
        patch.object(summariser.settings.ai_summary, "enabled", True),
        patch("terrapod.services.summariser.get_db_session") as mock_session,
        patch("terrapod.services.summariser._upsert_summary", upsert),
        patch("terrapod.services.summariser._call_model") as call_model,
    ):
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await summariser.handle_ai_plan_summary({"run_id": str(run.id), "kind": "plan_summary"})

        call_model.assert_not_called()
        upsert.assert_awaited_once()
        kwargs = upsert.await_args.kwargs
        assert kwargs["status"] == "skipped"
        assert "workspace disabled" in kwargs["error_message"]


async def test_handler_rejects_bad_payload():
    with patch.object(summariser.settings.ai_summary, "enabled", True):
        # Missing run_id, malformed uuid — must not raise
        await summariser.handle_ai_plan_summary({})
        await summariser.handle_ai_plan_summary({"run_id": "not-a-uuid"})


async def test_handler_rejects_unknown_kind():
    with patch.object(summariser.settings.ai_summary, "enabled", True):
        await summariser.handle_ai_plan_summary({"run_id": str(uuid.uuid4()), "kind": "bogus"})


async def test_handler_success_path_writes_ready_row():
    run = _mock_run()
    ws = _mock_workspace(mode="default", ctx="vault prod", ws_id=run.workspace_id)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=ws)),
        ]
    )
    mock_db.commit = AsyncMock()

    upsert = AsyncMock()
    call_model = AsyncMock(
        return_value=(
            {
                "description": "Changes the VPC.",
                "risk_level": "medium",
                "risk_factors": [{"severity": "medium", "title": "T", "detail": "D"}],
            },
            120,
            55,
        )
    )

    with (
        patch.object(summariser.settings.ai_summary, "enabled", True),
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch.object(summariser.settings.ai_summary, "daily_token_budget", 0),
        patch("terrapod.services.summariser.get_db_session") as mock_session,
        patch("terrapod.services.summariser._upsert_summary", upsert),
        patch("terrapod.services.summariser._call_model", call_model),
        patch(
            "terrapod.services.summariser._gather_inputs",
            AsyncMock(return_value=('{"resource_changes": []}', "PLAN_JSON", "json", "")),
        ),
        patch("terrapod.services.summariser._emit_ready_event", AsyncMock()),
    ):
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await summariser.handle_ai_plan_summary({"run_id": str(run.id), "kind": "plan_summary"})

        call_model.assert_awaited_once()
        upsert.assert_awaited_once()
        kwargs = upsert.await_args.kwargs
        assert kwargs["status"] == "ready"
        assert kwargs["risk_level"] == "medium"
        assert kwargs["output_tokens"] == 55


async def test_handler_call_failure_writes_errored_row():
    run = _mock_run()
    ws = _mock_workspace(mode="default", ws_id=run.workspace_id)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=ws)),
        ]
    )
    mock_db.commit = AsyncMock()

    upsert = AsyncMock()
    with (
        patch.object(summariser.settings.ai_summary, "enabled", True),
        patch.object(summariser.settings.ai_summary, "daily_token_budget", 0),
        patch("terrapod.services.summariser.get_db_session") as mock_session,
        patch("terrapod.services.summariser._upsert_summary", upsert),
        patch(
            "terrapod.services.summariser._gather_inputs",
            AsyncMock(return_value=("plan_json", "PLAN_JSON", "json", "")),
        ),
        patch(
            "terrapod.services.summariser._call_model",
            AsyncMock(side_effect=RuntimeError("upstream 500")),
        ),
    ):
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await summariser.handle_ai_plan_summary({"run_id": str(run.id), "kind": "plan_summary"})

        upsert.assert_awaited_once()
        kwargs = upsert.await_args.kwargs
        assert kwargs["status"] == "errored"
        assert "upstream 500" in kwargs["error_message"]


async def test_handler_missing_primary_input_writes_errored_row():
    run = _mock_run()
    ws = _mock_workspace(mode="default", ws_id=run.workspace_id)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=ws)),
        ]
    )
    mock_db.commit = AsyncMock()

    upsert = AsyncMock()
    with (
        patch.object(summariser.settings.ai_summary, "enabled", True),
        patch.object(summariser.settings.ai_summary, "daily_token_budget", 0),
        patch("terrapod.services.summariser.get_db_session") as mock_session,
        patch("terrapod.services.summariser._upsert_summary", upsert),
        patch(
            "terrapod.services.summariser._gather_inputs",
            AsyncMock(return_value=("", "PLAN_JSON", "json", "")),
        ),
        patch("terrapod.services.summariser._call_model") as call_model,
    ):
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await summariser.handle_ai_plan_summary({"run_id": str(run.id), "kind": "plan_summary"})

        call_model.assert_not_called()
        upsert.assert_awaited_once()
        assert upsert.await_args.kwargs["status"] == "errored"


async def test_handler_normalises_invalid_risk_level():
    """Model returns an out-of-enum risk_level — handler clamps to 'low'."""
    run = _mock_run()
    ws = _mock_workspace(mode="default", ws_id=run.workspace_id)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=ws)),
        ]
    )
    mock_db.commit = AsyncMock()

    upsert = AsyncMock()
    with (
        patch.object(summariser.settings.ai_summary, "enabled", True),
        patch.object(summariser.settings.ai_summary, "daily_token_budget", 0),
        patch("terrapod.services.summariser.get_db_session") as mock_session,
        patch("terrapod.services.summariser._upsert_summary", upsert),
        patch(
            "terrapod.services.summariser._gather_inputs",
            AsyncMock(return_value=("{}", "PLAN_JSON", "json", "")),
        ),
        patch(
            "terrapod.services.summariser._call_model",
            AsyncMock(
                return_value=(
                    {"description": "x", "risk_level": "CATASTROPHIC", "risk_factors": []},
                    10,
                    5,
                )
            ),
        ),
        patch("terrapod.services.summariser._emit_ready_event", AsyncMock()),
    ):
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

        await summariser.handle_ai_plan_summary({"run_id": str(run.id), "kind": "plan_summary"})

        assert upsert.await_args.kwargs["risk_level"] == "low"
