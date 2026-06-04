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


def _fake_tool_call(arguments):
    """Build a MagicMock shaped like LiteLLM's tool_call entry."""
    tc = MagicMock()
    tc.function.arguments = arguments
    return tc


def _fake_response(*, tool_calls=None, content="", finish_reason="stop", in_tok=10, out_tok=20):
    """Build a MagicMock shaped like LiteLLM's `ModelResponse.choices[0]`."""
    choice = MagicMock()
    choice.message.tool_calls = tool_calls
    choice.message.content = content
    choice.finish_reason = finish_reason
    return MagicMock(
        choices=[choice],
        usage=MagicMock(prompt_tokens=in_tok, completion_tokens=out_tok),
    )


async def test_call_model_raises_on_finish_length():
    """Truncation must surface as a specific error, regardless of whether
    we got a partial tool-call or partial body content.
    """
    resp = _fake_response(content='{"partial"', finish_reason="length")
    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch("terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=resp)),
    ):
        with pytest.raises(RuntimeError, match="truncated at max_output_tokens"):
            await summariser._call_model(
                kind="plan_summary",
                system_message="s",
                user_message="u",
                max_output_tokens=1024,
            )


async def test_call_model_parses_tool_call_arguments_string():
    """Happy path: provider returns the tool call with arguments as a
    JSON string (OpenAI native shape, also what Bedrock Converse for
    Anthropic produces via LiteLLM's translation).
    """
    args_str = '{"description":"adds a VPC","risk_level":"low","risk_factors":[]}'
    resp = _fake_response(tool_calls=[_fake_tool_call(args_str)])
    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch("terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=resp)),
    ):
        parsed, in_tok, out_tok = await summariser._call_model(
            kind="plan_summary",
            system_message="s",
            user_message="u",
            max_output_tokens=1024,
        )
    assert parsed["description"] == "adds a VPC"
    assert parsed["risk_level"] == "low"
    assert in_tok == 10
    assert out_tok == 20


async def test_call_model_parses_tool_call_arguments_dict():
    """Some LiteLLM provider translations return `arguments` as a dict
    rather than a JSON string. Both shapes must work.
    """
    args_dict = {
        "description": "deletes a Lambda",
        "risk_level": "medium",
        "risk_factors": [],
    }
    resp = _fake_response(tool_calls=[_fake_tool_call(args_dict)])
    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch("terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=resp)),
    ):
        parsed, *_ = await summariser._call_model(
            kind="plan_summary",
            system_message="s",
            user_message="u",
            max_output_tokens=1024,
        )
    assert parsed == args_dict


async def test_call_model_falls_back_to_body_when_no_tool_calls():
    """If a provider ignores tool_choice and replies in prose, we fall
    back to the legacy `_parse_model_json` path. Defensive — shouldn't
    happen with constrained-decode providers but keeps self-hosted
    backends working.
    """
    body = '{"description":"fallback path","risk_level":"low","risk_factors":[]}'
    resp = _fake_response(tool_calls=None, content=body)
    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch("terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=resp)),
    ):
        parsed, *_ = await summariser._call_model(
            kind="plan_summary",
            system_message="s",
            user_message="u",
            max_output_tokens=1024,
        )
    assert parsed["description"] == "fallback path"


async def test_call_model_surfaces_malformed_tool_arguments():
    """If the provider somehow returns a malformed-JSON string for the
    tool arguments (shouldn't happen, but defensive), surface as an
    actionable ValueError that mentions the JSON cause.
    """
    bad = '{"description":"truncated mid'  # unterminated string + obj
    resp = _fake_response(tool_calls=[_fake_tool_call(bad)])
    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch("terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=resp)),
    ):
        with pytest.raises(ValueError, match="tool-call arguments invalid JSON"):
            await summariser._call_model(
                kind="plan_summary",
                system_message="s",
                user_message="u",
                max_output_tokens=1024,
            )


async def test_call_model_fallback_parse_failure_includes_finish_reason():
    """When the body-fallback path itself fails to parse, surface
    finish_reason + length so the operator can tell from the run UI
    whether they got a refusal, a truncation, or a malformed JSON
    blob from a provider that ignored tools.
    """
    resp = _fake_response(
        tool_calls=None, content="I cannot summarise this plan.", finish_reason="stop"
    )
    with (
        patch.object(summariser.settings.ai_summary, "model", "test-model"),
        patch("terrapod.services.summariser.litellm.acompletion", AsyncMock(return_value=resp)),
    ):
        with pytest.raises(ValueError, match="finish_reason=stop"):
            await summariser._call_model(
                kind="plan_summary",
                system_message="s",
                user_message="u",
                max_output_tokens=1024,
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
            kind="plan_summary",
            system_message="sys",
            user_message="usr",
            max_output_tokens=100,
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
            kind="plan_summary",
            system_message="sys",
            user_message="usr",
            max_output_tokens=100,
        )
        assert "aws_role_name" not in kw
        assert "aws_external_id" not in kw


def test_build_litellm_kwargs_includes_tool_choice_for_plan_summary():
    """Tool-calling is the canonical structured-output path. The kwargs
    must include the submit_plan_summary tool AND force it via
    tool_choice — without the force, models can opt to reply in prose.
    """
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
            kind="plan_summary",
            system_message="sys",
            user_message="usr",
            max_output_tokens=100,
        )
        assert isinstance(kw["tools"], list) and len(kw["tools"]) == 1
        tool = kw["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "submit_plan_summary"
        # Schema lands on the tool, not in the user message.
        assert "description" in tool["function"]["parameters"]["properties"]
        assert "risk_level" in tool["function"]["parameters"]["properties"]
        assert kw["tool_choice"] == {
            "type": "function",
            "function": {"name": "submit_plan_summary"},
        }


def test_build_litellm_kwargs_uses_failure_analysis_tool_for_failure_kind():
    """failure_analysis kind selects the submit_failure_analysis tool
    (same JSON schema, different name + description). The wrong tool
    would confuse the model about whether it's summarising or analysing.
    """
    with (
        patch.object(summariser.settings.ai_summary, "model", "openai/gpt-5"),
        patch.object(summariser.settings.ai_summary, "api_base", ""),
        patch.object(summariser.settings.ai_summary.auth, "api_key", "sk-x"),
        patch.object(summariser.settings.ai_summary.auth, "aws_region", "us-east-1"),
        patch.object(summariser.settings.ai_summary.auth, "aws_role_arn", ""),
        patch.object(summariser.settings.ai_summary.auth, "aws_session_name", "x"),
        patch.object(summariser.settings.ai_summary.auth, "aws_external_id", ""),
    ):
        kw = summariser._build_litellm_kwargs(
            kind="failure_analysis",
            system_message="sys",
            user_message="usr",
            max_output_tokens=100,
        )
        assert kw["tools"][0]["function"]["name"] == "submit_failure_analysis"
        assert kw["tool_choice"]["function"]["name"] == "submit_failure_analysis"


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
            kind="plan_summary",
            system_message="sys",
            user_message="usr",
            max_output_tokens=100,
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
            AsyncMock(return_value=('{"resource_changes": []}', "PLAN_JSON", "json", "", "")),
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
            AsyncMock(return_value=("plan_json", "PLAN_JSON", "json", "", "")),
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
            AsyncMock(return_value=("", "PLAN_JSON", "json", "", "")),
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
            AsyncMock(return_value=("{}", "PLAN_JSON", "json", "", "")),
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


# ── _clean_plan_json_bytes (#406 / v0.30.4) ───────────────────────────────


class TestCleanPlanJsonBytes:
    """The cleaner physically removes definitionally-uninformative noise so
    no-op snapshots cannot be hallucinated as changes.

    Rules under test:
      • drop resource_changes with actions ⊆ {no-op, read} AND no
        `importing`
      • keep import-via-no-op
      • drop output_changes with actions == ["no-op"]
      • drop top-level `prior_state`
      • preserve `resource_drift` untouched
      • degrade gracefully on malformed input
    """

    def _clean(self, plan: dict) -> dict:
        import json as _json

        out = summariser._clean_plan_json_bytes(_json.dumps(plan).encode())
        return _json.loads(out)

    def test_drops_pure_noop_resource_change(self):
        plan = {
            "resource_changes": [
                {"address": "x", "change": {"actions": ["no-op"]}},
                {"address": "y", "change": {"actions": ["update"]}},
            ]
        }
        out = self._clean(plan)
        addrs = [r["address"] for r in out["resource_changes"]]
        assert addrs == ["y"]

    def test_drops_pure_read_resource_change(self):
        plan = {
            "resource_changes": [
                {"address": "ds", "change": {"actions": ["read"]}},
                {"address": "y", "change": {"actions": ["delete"]}},
            ]
        }
        out = self._clean(plan)
        addrs = [r["address"] for r in out["resource_changes"]]
        assert addrs == ["y"]

    def test_keeps_noop_with_import(self):
        """State-only adoption shows as actions=["no-op"] + importing set."""
        plan = {
            "resource_changes": [
                {
                    "address": "vault.ns",
                    "change": {
                        "actions": ["no-op"],
                        "importing": {"id": "vault"},
                    },
                },
            ]
        }
        out = self._clean(plan)
        assert len(out["resource_changes"]) == 1
        assert out["resource_changes"][0]["address"] == "vault.ns"

    def test_keeps_create_update_delete_and_replace(self):
        plan = {
            "resource_changes": [
                {"address": "c", "change": {"actions": ["create"]}},
                {"address": "u", "change": {"actions": ["update"]}},
                {"address": "d", "change": {"actions": ["delete"]}},
                {"address": "r", "change": {"actions": ["create", "delete"]}},
                {"address": "noop", "change": {"actions": ["no-op"]}},
            ]
        }
        out = self._clean(plan)
        addrs = sorted(r["address"] for r in out["resource_changes"])
        assert addrs == ["c", "d", "r", "u"]

    def test_drops_noop_output_changes(self):
        plan = {
            "output_changes": {
                "stable": {"actions": ["no-op"]},
                "changed": {"actions": ["update"]},
            }
        }
        out = self._clean(plan)
        assert "stable" not in out["output_changes"]
        assert "changed" in out["output_changes"]

    def test_drops_prior_state(self):
        plan = {"prior_state": {"big": "snapshot"}, "resource_changes": []}
        out = self._clean(plan)
        assert "prior_state" not in out

    def test_partitions_drift_reverted_vs_observed_only(self):
        """resource_drift is split based on whether each address ALSO has
        a real resource_changes entry:
          - matched → stays in resource_drift (the apply IS reverting it,
            elevated risk)
          - unmatched → moved to drift_observed_no_apply_action with
            actions rewritten to ["drift_observed"] so the model can't
            pattern-match destroy framing onto it.
        """
        plan = {
            "resource_drift": [
                # matched: also in resource_changes — drift being reverted
                {"address": "drift_a", "change": {"actions": ["update"]}},
                # unmatched: no resource_changes — accepted, apply no-ops
                {"address": "drift_b", "change": {"actions": ["delete"]}},
            ],
            "resource_changes": [
                {"address": "drift_a", "change": {"actions": ["update"]}},
            ],
        }
        out = self._clean(plan)
        assert out["resource_drift"] == [
            {"address": "drift_a", "change": {"actions": ["update"]}},
        ]
        assert out["drift_observed_no_apply_action"] == [
            {"address": "drift_b", "change": {"actions": ["drift_observed"]}},
        ]

    def test_drift_without_resource_changes_all_moves(self):
        """When no resource_changes match, every drift entry moves to
        drift_observed_no_apply_action and its actions are neutralised.
        Reproduces the failure mode from the prod-us2-services1 AKS
        plan summary: node pools missing in Azure showed as drift
        deletes, model hallucinated them as planned destroys.
        """
        plan = {
            "resource_drift": [
                {
                    "address": 'module.aks.azurerm_kubernetes_cluster_node_pool.this["gpu0"]',
                    "change": {"actions": ["delete"]},
                },
                {
                    "address": 'module.aks.azurerm_kubernetes_cluster_node_pool.this["pulsar1"]',
                    "change": {"actions": ["delete"]},
                },
            ],
            "resource_changes": [],
        }
        out = self._clean(plan)
        assert out["resource_drift"] == []
        assert [d["address"] for d in out["drift_observed_no_apply_action"]] == [
            'module.aks.azurerm_kubernetes_cluster_node_pool.this["gpu0"]',
            'module.aks.azurerm_kubernetes_cluster_node_pool.this["pulsar1"]',
        ]
        for d in out["drift_observed_no_apply_action"]:
            assert d["change"]["actions"] == ["drift_observed"]

    def test_drift_partition_skips_noop_resource_changes(self):
        """no-op resource_changes are dropped first; the drift partition
        must run against the pruned set, so a drift entry matched only
        by a no-op resource_change is moved to drift_observed (apply
        does nothing about it).
        """
        plan = {
            "resource_drift": [
                {"address": "addr_a", "change": {"actions": ["delete"]}},
            ],
            "resource_changes": [
                {"address": "addr_a", "change": {"actions": ["no-op"]}},
            ],
        }
        out = self._clean(plan)
        assert out["resource_drift"] == []
        assert out["drift_observed_no_apply_action"] == [
            {"address": "addr_a", "change": {"actions": ["drift_observed"]}},
        ]

    def test_no_drift_observed_key_when_no_unmatched(self):
        """Don't emit drift_observed_no_apply_action when there's nothing
        to put in it — keeps the input minimal.
        """
        plan = {
            "resource_drift": [
                {"address": "a", "change": {"actions": ["update"]}},
            ],
            "resource_changes": [
                {"address": "a", "change": {"actions": ["update"]}},
            ],
        }
        out = self._clean(plan)
        assert out["resource_drift"] == plan["resource_drift"]
        assert "drift_observed_no_apply_action" not in out

    def test_keeps_weird_actions_shape(self):
        """Defensive: never drop a resource_change whose shape we don't
        recognise. Better to over-include than silently lose data.
        """
        plan = {
            "resource_changes": [
                {"address": "weird1", "change": {"actions": None}},
                {"address": "weird2", "change": {}},
                {"address": "weird3", "change": "not a dict"},
                {"address": "weird4"},  # no change key
            ]
        }
        out = self._clean(plan)
        addrs = sorted(r["address"] for r in out["resource_changes"])
        assert addrs == ["weird1", "weird2", "weird3", "weird4"]

    def test_malformed_json_returns_unchanged(self):
        raw = b"not json at all"
        assert summariser._clean_plan_json_bytes(raw) == raw

    def test_non_dict_top_level_returns_unchanged(self):
        raw = b"[1, 2, 3]"
        assert summariser._clean_plan_json_bytes(raw) == raw

    def test_drops_in_real_world_shape(self):
        """End-to-end: cluster + node-group + log-group all no-ops with
        the snapshot fields that previously confused the model.
        """
        plan = {
            "format_version": "1.2",
            "resource_changes": [
                {
                    "address": "module.eks.aws_eks_cluster.this[0]",
                    "type": "aws_eks_cluster",
                    "change": {
                        "actions": ["no-op"],
                        "before": {"version": "1.35"},
                        "after": {"version": "1.35"},
                    },
                },
                {
                    "address": "module.eks.aws_eks_node_group.ng1",
                    "type": "aws_eks_node_group",
                    "change": {
                        "actions": ["no-op"],
                        "before": {"version": "1.35"},
                        "after": {"version": "1.35"},
                    },
                },
                {
                    "address": "module.vpc.aws_subnet.private[3]",
                    "type": "aws_subnet",
                    "change": {
                        "actions": ["update"],
                        "before": {"map_public_ip_on_launch": True},
                        "after": {"map_public_ip_on_launch": False},
                    },
                },
            ],
        }
        out = self._clean(plan)
        addrs = [r["address"] for r in out["resource_changes"]]
        assert addrs == ["module.vpc.aws_subnet.private[3]"]
        # The cluster's "version: 1.35" snapshot — the exact field that
        # confabulated the v0.30.3 hallucination — is GONE from the
        # bytes the model will see. Hard guarantee, not prompt-based.
        import json as _json

        out_str = _json.dumps(out)
        assert "aws_eks_cluster" not in out_str
        assert "aws_eks_node_group" not in out_str


# ── _build_code_diff (#406 / v0.30.4) ────────────────────────────────────


class TestBuildCodeDiff:
    """CODE_DIFF is best-effort context. The contract is:
    • return "" on every plausible failure
    • return a unified diff (with `+`/`-` lines) when both tarballs
      exist and differ on *.tf / *.tfvars files
    • only diff .tf / .tfvars — README.md, .terraform/, etc. don't count
    • respect the byte cap
    """

    def test_returns_empty_when_prev_tarball_none(self):
        cur = _build_tarball({"main.tf": b'resource "x" "y" {}'})
        assert summariser._build_code_diff(None, cur, 100_000) == ""

    def test_returns_empty_when_max_bytes_zero(self):
        cur = _build_tarball({"main.tf": b'resource "x" "y" {}'})
        prev = _build_tarball({"main.tf": b"# different"})
        assert summariser._build_code_diff(prev, cur, 0) == ""

    def test_returns_empty_when_tarballs_identical(self):
        same = _build_tarball({"main.tf": b'resource "x" "y" {}'})
        # Use literal-identical bytes for both sides → diff must be empty.
        out = summariser._build_code_diff(same, same, 100_000)
        assert out == ""

    def test_returns_diff_when_tf_changed(self):
        prev = _build_tarball(
            {"main.tf": b'resource "aws_vpc" "this" {\n  cidr_block = "10.0.0.0/16"\n}\n'}
        )
        cur = _build_tarball(
            {"main.tf": b'resource "aws_vpc" "this" {\n  cidr_block = "10.1.0.0/16"\n}\n'}
        )
        out = summariser._build_code_diff(prev, cur, 100_000)
        assert out != ""
        # Both sides of the change appear in the unified diff
        assert "10.0.0.0/16" in out
        assert "10.1.0.0/16" in out
        # Standard unified-diff markers
        assert "---" in out
        assert "+++" in out

    def test_ignores_non_tf_files(self):
        prev = _build_tarball(
            {
                "main.tf": b'resource "x" "y" {}',
                "README.md": b"old readme",
            }
        )
        cur = _build_tarball(
            {
                "main.tf": b'resource "x" "y" {}',
                "README.md": b"WILDLY DIFFERENT README CONTENT",
            }
        )
        # Only README.md changed; *.tf is identical. Should be empty.
        out = summariser._build_code_diff(prev, cur, 100_000)
        assert out == ""

    def test_includes_tfvars(self):
        prev = _build_tarball({"terraform.tfvars": b'env = "dev"\n'})
        cur = _build_tarball({"terraform.tfvars": b'env = "prod"\n'})
        out = summariser._build_code_diff(prev, cur, 100_000)
        assert "dev" in out
        assert "prod" in out

    def test_corrupt_prev_tarball_returns_empty(self):
        cur = _build_tarball({"main.tf": b'resource "x" "y" {}'})
        out = summariser._build_code_diff(b"not a tarball", cur, 100_000)
        assert out == ""

    def test_corrupt_cur_tarball_returns_empty(self):
        prev = _build_tarball({"main.tf": b'resource "x" "y" {}'})
        out = summariser._build_code_diff(prev, b"not a tarball", 100_000)
        assert out == ""

    def test_path_traversal_member_skipped(self):
        """A tarball containing `../../etc/passwd` must not write outside
        the temp dir. The malicious member is silently skipped.
        """
        prev = _build_tarball({"main.tf": b"# v1"})
        # Build a tarball that includes a traversal entry alongside a
        # legitimate .tf file. The legitimate .tf is what should be
        # used for the diff; the traversal entry must be dropped.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in [
                ("../escape.tf", b"# escape attempt"),
                ("main.tf", b"# v2"),
            ]:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        cur = buf.getvalue()
        out = summariser._build_code_diff(prev, cur, 100_000)
        assert out != ""
        assert "# v1" in out
        assert "# v2" in out
        assert "escape attempt" not in out  # never extracted

    def test_diff_truncated_at_max_bytes(self):
        # Construct a large diff by changing a single line many times
        prev_content = ("a\n" * 5000).encode()
        cur_content = ("b\n" * 5000).encode()
        prev = _build_tarball({"main.tf": prev_content})
        cur = _build_tarball({"main.tf": cur_content})
        out = summariser._build_code_diff(prev, cur, 1000)
        assert len(out) <= 1000 + 100  # 1000 cap + truncation marker
        assert "truncated from tail" in out
