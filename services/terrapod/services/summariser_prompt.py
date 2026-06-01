"""Skill prompt for the AI plan-summary feature (#401).

This module owns the in-code system prompt that defines the model's task,
the structured output contract, and the safety guardrails. The DB schema,
SSE event shape, and UI rendering all assume the JSON schema declared
here — changing the schema means changing all three.

Helm-provided `prompt_prefix` and `prompt_suffix` strings wrap this skill
prompt; they are intended for tone/emphasis tweaks, NOT for changing the
output contract. See `AISummaryContextConfig` in `config.py`.
"""

from __future__ import annotations

# Single source of truth for the schema. The JSON-schema dict is sent in
# the request `response_format` and the prose schema is also embedded in
# the user message — Bedrock OpenAI-compat ignores `response_format` for
# some Anthropic models, so the in-prompt schema is the durable backstop.
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


PLAN_SUMMARY_SKILL_PROMPT = """\
You are a Terraform plan reviewer embedded in Terrapod. You receive the
proposed changes from a terraform/tofu plan and the HCL that produced
them. Your job is to summarise what the plan changes and rate the risk
of those changes. Nothing else.

You will receive these inputs in the user message:
  • PLAN_JSON — `tofu show -json` output for the proposed changes.
  • CODE_CONTEXT — concatenated .tf source. May be absent.
  • FLEET_CONTEXT — deployment-wide notes from the operator. May be empty.
  • WORKSPACE_CONTEXT — workspace-specific notes. May be empty.

You return a single JSON object matching this schema, with no
surrounding text, markdown fences, or commentary:

  {
    "description": "<plain language summary of what changes, ~600 words max>",
    "risk_level": "<low | medium | high | critical>",
    "risk_factors": [
      {
        "severity": "<low | medium | high | critical>",
        "title": "<short label, max 120 chars>",
        "detail": "<explanation, max 600 chars>",
        "resource_address": "<terraform address, optional>"
      }
    ]
  }

Rules:

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
  • Do not invent resources or addresses not in the plan.
  • Risk severities are about blast radius and reversibility, not
    novelty. Destroying a Lambda is medium. Destroying a database or
    an IAM trust root is critical. Pure additions are low.

Style:
  • Operator-facing, terse, professional. No emojis. No first-person
    narration ("I will...", "Let me...").
  • Markdown allowed inside `description` only (lists, backticks for
    identifiers). Plain strings everywhere else.
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
  • CODE_CONTEXT — concatenated .tf source. May be absent.
  • FLEET_CONTEXT — deployment-wide notes. May be empty.
  • WORKSPACE_CONTEXT — workspace-specific notes. May be empty.

You return a single JSON object matching this schema, with no
surrounding text, markdown fences, or commentary:

  {
    "description": "<plain language explanation of what went wrong, ~600 words max>",
    "risk_level": "<low | medium | high | critical>",
    "risk_factors": [
      {
        "severity": "<low | medium | high | critical>",
        "title": "<short fix label, max 120 chars>",
        "detail": "<concrete steps or change to make, max 600 chars>",
        "resource_address": "<terraform address, optional>"
      }
    ]
  }

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
  • Markdown allowed inside `description` only.
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
    prompt_prefix: str = "",
    prompt_suffix: str = "",
) -> tuple[str, str]:
    """Render the system + user messages for the Chat Completions request.

    Returns ``(system_message, user_message)``. The system message owns
    the contract (skill prompt + operator's prefix/suffix); the user
    message carries the deployment-specific context plus the primary
    input (PLAN_JSON or PLAN_LOG) and the code.

    Args:
        kind: "plan_summary" or "failure_analysis" — selects the skill.
        primary_input: the truncated plan JSON or plan log.
        primary_input_label: header label ("PLAN_JSON" or "PLAN_LOG").
        primary_input_lang: fenced-code language ("json" or "text").
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
    if code_context_truncated.strip():
        user_parts.append(f"CODE_CONTEXT:\n```hcl\n{code_context_truncated}\n```")

    user_parts.append(
        "Now produce the JSON object per the schema in the system prompt. "
        "Output JSON only — no surrounding text or code fences."
    )

    user_message = "\n\n".join(user_parts)
    return system_message, user_message
