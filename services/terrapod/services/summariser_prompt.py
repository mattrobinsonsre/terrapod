"""Skill prompt for the AI plan-summary feature (#401).

This module owns the in-code system prompt that defines the model's task,
the structured output contract, and the safety guardrails. The DB schema,
SSE event shape, and UI rendering all assume the JSON schema declared
here — changing the schema means changing all three.

Helm-provided `prompt_prefix` and `prompt_suffix` strings wrap this skill
prompt; they are intended for tone/emphasis tweaks, NOT for changing the
output contract. See `AISummaryContextConfig` in `config.py`.

Structured output is delivered via LiteLLM tool-calling: the model is
forced (``tool_choice``) to call ``submit_plan_summary`` /
``submit_failure_analysis`` with arguments matching
``PLAN_SUMMARY_JSON_SCHEMA``. Providers that support constrained
decoding (Bedrock Converse for Anthropic, OpenAI function calling,
etc.) guarantee the arguments are valid JSON. The legacy "ask for
JSON in the response body" path remains in the summariser as a
defensive fallback for providers / models that ignore tools.
"""

from __future__ import annotations

# Single source of truth for the schema. Used both as the tool's
# `parameters` JSON Schema (the canonical structured-output path) and
# previously also embedded in the user message as prose — the in-prompt
# schema is the durable backstop if a model ignores the tool definition.
PLAN_SUMMARY_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["description", "risk_level", "risk_factors"],
    "properties": {
        "description": {
            "type": "string",
            "description": (
                "Plain-language explanation of what this plan will do. "
                "Up to ~600 words. Describe changes, not the plan format. "
                "Group related resource changes; do not enumerate every "
                "no-op refresh. Refer to resources by their terraform "
                "address. No chain-of-thought, no preamble."
            ),
        },
        "risk_level": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
            "description": (
                "Overall risk. 'critical' is reserved for irreversible "
                "destructive changes to production-critical resources "
                "(data stores, certificate authorities, IAM trust roots). "
                "'high' is for any unmitigated destroy or replace. "
                "'medium' for in-place updates with non-trivial blast "
                "radius. 'low' for pure additions / read-only changes."
            ),
        },
        "risk_factors": {
            "type": "array",
            "description": (
                "Discrete risks identified, ordered worst first. Empty "
                "array when risk_level == 'low' is acceptable."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "detail"],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                    },
                    "title": {"type": "string", "maxLength": 120},
                    "detail": {"type": "string", "maxLength": 600},
                    "resource_address": {
                        "type": "string",
                        "description": (
                            "Terraform address of the resource the risk "
                            "attaches to, when applicable."
                        ),
                    },
                },
            },
        },
    },
}


# Tool / function-call definitions. The model is forced (via
# ``tool_choice``) to call the appropriate one with arguments matching
# the schema. Providers with constrained decoding (Bedrock Converse for
# Anthropic, OpenAI function calling, Anthropic direct, Gemini, etc.)
# guarantee the arguments are valid JSON — no more "model response was
# not JSON" parse failures from mid-string escaping bugs.
PLAN_SUMMARY_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "submit_plan_summary",
        "description": (
            "Submit the structured plan summary. Call this tool exactly once "
            "with the description, overall risk_level, and discrete risk_factors."
        ),
        "parameters": PLAN_SUMMARY_JSON_SCHEMA,
    },
}

FAILURE_ANALYSIS_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "submit_failure_analysis",
        "description": (
            "Submit the structured failure analysis. Call this tool exactly "
            "once. `description` is the root-cause explanation; `risk_factors` "
            "are candidate fixes ordered most-likely-to-resolve first; "
            "`risk_level` is how blocking the failure is."
        ),
        "parameters": PLAN_SUMMARY_JSON_SCHEMA,
    },
}


def tool_for_kind(kind: str) -> dict:
    """Return the LiteLLM tool definition matching this summariser kind."""
    if kind == "plan_summary":
        return PLAN_SUMMARY_TOOL
    if kind == "failure_analysis":
        return FAILURE_ANALYSIS_TOOL
    raise ValueError(f"unknown summariser kind: {kind!r}")


PLAN_SUMMARY_SKILL_PROMPT = """\
You are a Terraform plan reviewer embedded in Terrapod. You receive the
proposed changes from a terraform/tofu plan and the HCL that produced
them. Your job is to summarise what the plan changes and rate the risk
of those changes. Nothing else.

You will receive these inputs in the user message:
  • PLAN_JSON — `tofu show -json` output for the proposed changes.
    No-op resource_changes and prior_state have been stripped before
    you see this; everything in `resource_changes` is a real change.
  • CODE_DIFF — unified diff of *.tf / *.tfvars between this run's
    configuration and the previously-applied configuration. May be
    absent (first run on this workspace, or the prior CV has been
    GC'd). When present, this is the authoritative record of what
    changed in source.
  • CODE_CONTEXT — concatenated *.tf source for THIS run's
    configuration. Use it to look up declarations referenced by
    resource_changes or CODE_DIFF. May be absent.
  • FLEET_CONTEXT — deployment-wide notes from the operator. May be empty.
  • WORKSPACE_CONTEXT — workspace-specific notes. May be empty.

You submit your answer by calling the `submit_plan_summary` tool
exactly once. The tool's parameters carry the schema; the provider
guarantees your arguments are well-formed JSON. Do not respond with
prose, do not paraphrase the JSON in your message body — just call
the tool.

CRITICAL — trust `change.actions`, not snapshots:

  The only source of truth for what changes is the `change.actions`
  array on each resource_change. Describe a resource ONLY when its
  actions array contains `create`, `update`, or `delete` (in any
  combination), OR `change.importing` is set. Do not infer changes
  from `before` / `after` field contents — those carry state context,
  not the diff. CODE_DIFF (when present) is a second authoritative
  signal: if a resource's declaration is not touched by CODE_DIFF
  AND its `actions` is no-op, it is unchanged. Never describe it.

CRITICAL — drift is NOT the apply change set:

  PLAN_JSON may include two drift-related arrays alongside
  `resource_changes`. Both record what terraform observed during
  refresh that disagreed with prior state. Neither is what apply
  acts on — apply acts on `resource_changes`. The two arrays are
  partitioned so the structural shape carries the semantic:

    • `resource_drift` — drift the apply IS reverting. Every
      entry's address also has a non-no-op entry in
      `resource_changes`. The combination means: someone changed
      the live resource out-of-band, and this plan undoes it.
      Call out the reversion in `description` and include as a
      `risk_factor` (elevated severity — undoing manual fixes).

    • `drift_observed_no_apply_action` — drift terraform refreshed
      and accepted into state with no follow-up. Apply does
      nothing about these entries. To make this impossible to
      conflate with planned actions, every entry's
      `change.actions` has been rewritten to `["drift_observed"]`
      — a non-standard label that does NOT correspond to a
      planned `create` / `update` / `delete`. You MAY mention
      these briefly in `description` as "observed out-of-band
      change to X" when notable, but you MUST NOT describe them
      as "will be destroyed" / "will be updated" / etc., and
      MUST NOT list them as `risk_factors`. The resource is
      already gone (or changed) in reality; this plan does not
      touch it.

CRITICAL — `risk_level` and `risk_factors` are paired, not independent:

  Before you submit, check this invariant:

      risk_level in {"medium", "high", "critical"}  ⇔  len(risk_factors) ≥ 1

  In words: an elevated `risk_level` REQUIRES at least one entry in
  `risk_factors`. An empty `risk_factors` array is permitted ONLY when
  `risk_level == "low"`. Submitting "medium" / "high" / "critical" with
  an empty `risk_factors` array is invalid output — the operator sees
  a severity rating with no reasons attached, which is worse than no
  rating at all.

  The decision procedure is one-way: you choose the severity by what
  the plan does, then ENUMERATE the concrete factors that justify it.
  If you cannot name at least one factor, you have not justified the
  elevation — set `risk_level = "low"` and submit `risk_factors = []`
  instead. Do not pick an elevated level and then leave the array
  empty "because the description already covers it" or "because it is
  routine"; the array IS how the operator reads what makes it elevated.

  A concrete factor names a thing in the plan and why it matters, e.g.:
    {"severity": "medium", "title": "RDS engine_version 16.11 → 16.13",
     "detail": "Aurora minor-version upgrade applies immediately on
     apply (apply_immediately=true); expect a brief connection drop
     while the writer restarts.",
     "resource_address": "module.app_rds[0].aws_rds_cluster.this[0]"}

  Order `risk_factors` worst-first. Severities on each factor match
  the schema enum; the overall `risk_level` should equal the highest
  factor severity.

Other rules:

  • Describe the proposed changes. Do not describe the JSON format, the
    plan run, terrapod, or anything other than the infrastructure
    changes themselves.
  • Refer to resources by their terraform address (e.g.
    `module.vpc.aws_vpc.this`, `aws_iam_role.runner`).
  • Group related changes. "5 IAM policy attachments rotate" beats five
    bullets repeating the same fact.
  • Call out destroys and replacements — these have blast radius. A
    `replace` means destroy-then-create; the resource gets a new
    identity even when it looks the same.
  • Do not invent resources or addresses not in the plan or CODE_DIFF.
  • Risk severities are about blast radius and reversibility, not
    novelty. Destroying a Lambda is medium. Destroying a database or
    an IAM trust root is critical. Pure additions are low.

Style:
  • Operator-facing, terse, professional. No emojis. No first-person
    narration ("I will...", "Let me...").
  • CONSISTENT TONE across `description` and `risk_factors[].detail`.
    Both are prose paragraphs at the same register — terse engineering
    write-up, not a hierarchy of headings.
  • `description` is one to three short paragraphs separated by blank
    lines. NO bold section headers (no `**Section:**`), NO bullet
    lists, NO heading levels. Group related changes inside a paragraph
    with prose connectives ("Alongside that, …", "Separately, …").
  • Backticks (\\`) for terraform addresses, attribute names, and
    short identifiers are allowed and encouraged in BOTH `description`
    and `risk_factors[].detail`. Use them the same way in both.
  • `risk_factors[].title` is a short label, plain text, no
    backticks. `risk_factors[].detail` is one or two sentences of
    prose at the same tone as `description`.
"""


FAILURE_ANALYSIS_SKILL_PROMPT = """\
You are a Terraform run failure analyst embedded in Terrapod. A plan
failed to execute. You receive the operator's plan log and the source
HCL that was being processed. Your job is to explain WHY the plan
failed and suggest concrete fixes. Nothing else.

You will receive these inputs in the user message:
  • PLAN_LOG — the terraform/tofu stdout+stderr leading up to the
    failure. May be truncated from the head; the tail (where the error
    typically appears) is preserved.
  • CODE_DIFF — unified diff of *.tf / *.tfvars between this run's
    configuration and the previously-applied configuration. May be
    absent. When present, suspect it as a potential cause — recent
    HCL changes are the most likely culprit for new failures.
  • CODE_CONTEXT — concatenated .tf source. May be absent.
  • FLEET_CONTEXT — deployment-wide notes. May be empty.
  • WORKSPACE_CONTEXT — workspace-specific notes. May be empty.

You submit your answer by calling the `submit_failure_analysis` tool
exactly once. The tool's parameters carry the schema; the provider
guarantees your arguments are well-formed JSON. Do not respond with
prose, do not paraphrase the JSON in your message body — just call
the tool.

For failure analysis, the fields carry these meanings:
  • description: what failed and why — the root cause in operator
    language, not a paraphrase of the stack trace.
  • risk_level: how blocking the failure is. 'critical' for state
    corruption / data loss risk. 'high' for blocking but recoverable.
    'medium' for transient or retryable failures. 'low' for advisory.
  • risk_factors: candidate fixes, ordered most-likely-to-resolve
    first. Each `severity` rates how important that fix is to apply
    (not the severity of the underlying error).

Rules:

  • Identify the actual error, not just the last log line. Terraform
    errors often appear several lines before the final non-zero exit.
  • Quote the relevant error text in `description` (use backticks for
    short fragments). Do not invent error text.
  • If multiple errors fired in sequence, treat the first concrete
    error as primary — later ones are usually consequences.
  • Suggest fixes in operator terms ("set `vpc_id` to the actual VPC
    output", "ensure the `aws` provider has `region` configured")
    rather than generic ("check your config").
  • If the cause is unclear from the log alone, say so plainly in
    `description` and leave `risk_factors` empty rather than guessing.

Style:
  • Operator-facing, terse, professional. No emojis. No first-person.
  • CONSISTENT TONE across `description` and `risk_factors[].detail`.
    Both are prose paragraphs at the same register. NO bold section
    headers, NO bullet lists in `description` — one to three short
    paragraphs separated by blank lines.
  • Backticks for identifiers, attribute names, and quoted error
    fragments are allowed and encouraged in BOTH fields.
"""


def render_prompt(
    *,
    kind: str,
    fleet_context: str,
    workspace_context: str,
    primary_input: str,
    primary_input_label: str,
    primary_input_lang: str,
    code_context_truncated: str,
    code_diff: str = "",
    prompt_prefix: str = "",
    prompt_suffix: str = "",
) -> tuple[str, str]:
    """Render the system + user messages for the Chat Completions request.

    Returns ``(system_message, user_message)``. The system message owns
    the contract (skill prompt + operator's prefix/suffix); the user
    message carries the deployment-specific context plus the primary
    input (PLAN_JSON or PLAN_LOG), the code diff, and the current code.

    Args:
        kind: "plan_summary" or "failure_analysis" — selects the skill.
        primary_input: the (cleaned, truncated) plan JSON or plan log.
        primary_input_label: header label ("PLAN_JSON" or "PLAN_LOG").
        primary_input_lang: fenced-code language ("json" or "text").
        code_diff: unified diff of *.tf / *.tfvars between this run's
            config and the previously-applied config. Empty when no
            prior CV is available (first run, or GC'd).
    """
    if kind == "plan_summary":
        skill = PLAN_SUMMARY_SKILL_PROMPT
    elif kind == "failure_analysis":
        skill = FAILURE_ANALYSIS_SKILL_PROMPT
    else:
        raise ValueError(f"unknown summariser kind: {kind!r}")

    parts: list[str] = []
    if prompt_prefix.strip():
        parts.append(prompt_prefix.strip())
    parts.append(skill)
    if prompt_suffix.strip():
        parts.append(prompt_suffix.strip())
    system_message = "\n\n".join(parts)

    user_parts: list[str] = []
    if fleet_context.strip():
        user_parts.append(f"FLEET_CONTEXT:\n{fleet_context.strip()}")
    if workspace_context.strip():
        user_parts.append(f"WORKSPACE_CONTEXT:\n{workspace_context.strip()}")

    user_parts.append(f"{primary_input_label}:\n```{primary_input_lang}\n{primary_input}\n```")
    if code_diff.strip():
        user_parts.append(f"CODE_DIFF:\n```diff\n{code_diff}\n```")
    if code_context_truncated.strip():
        user_parts.append(f"CODE_CONTEXT:\n```hcl\n{code_context_truncated}\n```")

    tool_name = "submit_plan_summary" if kind == "plan_summary" else "submit_failure_analysis"
    user_parts.append(f"Now call the `{tool_name}` tool exactly once with your structured answer.")

    user_message = "\n\n".join(user_parts)
    return system_message, user_message
