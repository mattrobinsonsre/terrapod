"""Unit tests for the AI plan-summary skill prompts (#401)."""

import json

import pytest

from terrapod.services.summariser_prompt import (
    FAILURE_ANALYSIS_SKILL_PROMPT,
    PLAN_SUMMARY_JSON_SCHEMA,
    PLAN_SUMMARY_SKILL_PROMPT,
    render_prompt,
)


def test_json_schema_is_valid_json_serialisable():
    # The schema dict travels in an HTTP request body — must round-trip.
    encoded = json.dumps(PLAN_SUMMARY_JSON_SCHEMA)
    assert json.loads(encoded) == PLAN_SUMMARY_JSON_SCHEMA


def test_json_schema_required_fields():
    required = set(PLAN_SUMMARY_JSON_SCHEMA["required"])
    assert required == {"description", "risk_level", "risk_factors"}


def test_render_plan_summary_returns_skill_in_system():
    system, user = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated="",
    )
    assert PLAN_SUMMARY_SKILL_PROMPT.strip() in system
    assert FAILURE_ANALYSIS_SKILL_PROMPT.strip() not in system
    assert "PLAN_JSON" in user


def test_render_failure_analysis_uses_failure_skill():
    system, user = render_prompt(
        kind="failure_analysis",
        fleet_context="",
        workspace_context="",
        primary_input="Error: missing provider",
        primary_input_label="PLAN_LOG",
        primary_input_lang="text",
        code_context_truncated="",
    )
    assert FAILURE_ANALYSIS_SKILL_PROMPT.strip() in system
    assert PLAN_SUMMARY_SKILL_PROMPT.strip() not in system
    assert "PLAN_LOG" in user


def test_render_unknown_kind_raises():
    with pytest.raises(ValueError):
        render_prompt(
            kind="bogus",
            fleet_context="",
            workspace_context="",
            primary_input="",
            primary_input_label="X",
            primary_input_lang="text",
            code_context_truncated="",
        )


def test_prefix_and_suffix_wrap_skill_prompt():
    system, _ = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated="",
        prompt_prefix="BE TERSE.",
        prompt_suffix="TRAILING NOTE.",
    )
    p_idx = system.index("BE TERSE.")
    skill_idx = system.index(PLAN_SUMMARY_SKILL_PROMPT.strip())
    s_idx = system.index("TRAILING NOTE.")
    assert p_idx < skill_idx < s_idx


def test_empty_prefix_suffix_omitted_from_system():
    system, _ = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated="",
        prompt_prefix="   ",  # whitespace-only must be stripped
        prompt_suffix="",
    )
    # No leading/trailing blank sections from empty layers
    assert not system.startswith("\n\n")
    assert not system.endswith("\n\n\n")


def test_context_layers_render_in_user_message():
    _, user = render_prompt(
        kind="plan_summary",
        fleet_context="FLEET: we run AWS only.",
        workspace_context="WS: vault production.",
        primary_input='{"resource_changes":[]}',
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated='resource "aws_vpc" "this" {}',
    )
    # Fleet then workspace then primary then code, in that order
    f_idx = user.index("FLEET_CONTEXT")
    w_idx = user.index("WORKSPACE_CONTEXT")
    p_idx = user.index("PLAN_JSON")
    c_idx = user.index("CODE_CONTEXT")
    assert f_idx < w_idx < p_idx < c_idx


def test_code_context_omitted_when_empty():
    _, user = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated="",
    )
    assert "CODE_CONTEXT" not in user


# ── code_diff (#406 / v0.30.4) ───────────────────────────────────────────


def test_code_diff_renders_between_primary_and_code_context():
    _, user = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated='resource "aws_vpc" "this" {}',
        code_diff="--- a/main.tf\n+++ b/main.tf\n@@ -1 +1 @@\n-old\n+new\n",
    )
    p_idx = user.index("PLAN_JSON")
    d_idx = user.index("CODE_DIFF")
    c_idx = user.index("CODE_CONTEXT")
    # Diff sits between primary input and the full-source context — the
    # model reads the change FIRST, then can look up declarations.
    assert p_idx < d_idx < c_idx
    # Rendered as a diff-fenced block
    assert "```diff" in user


def test_code_diff_omitted_when_empty():
    _, user = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated="",
        code_diff="",
    )
    assert "CODE_DIFF" not in user


def test_code_diff_described_in_skill_prompt():
    """The skill prompt must teach the model what CODE_DIFF is — without
    that doc the new field is just noise. Keeps prompt-input contract
    in sync with the renderer.
    """
    system, _ = render_prompt(
        kind="plan_summary",
        fleet_context="",
        workspace_context="",
        primary_input="{}",
        primary_input_label="PLAN_JSON",
        primary_input_lang="json",
        code_context_truncated="",
        code_diff="",
    )
    assert "CODE_DIFF" in system
    # And the model must be told about the actions rule (the v0.30.3
    # hallucination fix moves into the upstream prompt itself).
    assert "change.actions" in system
    # And resource_drift must be described as NOT the apply change set.
    assert "resource_drift" in system


def test_failure_analysis_skill_also_describes_code_diff():
    """Failure analysis benefits from CODE_DIFF too — the recent change
    is the most likely cause of a new failure.
    """
    system, _ = render_prompt(
        kind="failure_analysis",
        fleet_context="",
        workspace_context="",
        primary_input="ERROR: ...",
        primary_input_label="PLAN_LOG",
        primary_input_lang="text",
        code_context_truncated="",
    )
    assert "CODE_DIFF" in system
