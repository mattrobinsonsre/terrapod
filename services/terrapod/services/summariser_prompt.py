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
                    "category": {
                        "type": "string",
                        "enum": [
                            "security",
                            "reliability",
                            "cost",
                            "operations",
                            "scalability",
                            "change",
                            "other",
                        ],
                        "description": (
                            "Which review dimension this factor belongs to. "
                            "'change' = a risk of the change itself (the "
                            "original plan-review lens); the others are the "
                            "grounded design-review dimensions. Optional; omit "
                            "for a plain change risk."
                        ),
                    },
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


# --- Cost summary (#871) -----------------------------------------------------
# The AI *enhancement* of a run's cost estimate: a plain-language narrative of
# the (authoritative, engine-derived) figures + optional savings advisories. HARD
# provenance: the model is told never to restate/override the deterministic total, and
# every advisory dollar amount is an AI ESTIMATE (the API stamps
# `source: "ai-estimate"` on each advisory server-side; the schema does not let
# the model claim otherwise).
COST_SUMMARY_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["estimated_resources", "advisories"],
    "properties": {
        "estimated_resources": {
            "type": "array",
            "description": (
                "YOUR PRIMARY JOB. From your own knowledge, estimate the monthly "
                "cost of resources the deterministic pricing engine could NOT "
                "price — the ones listed under UNPRICED (unmapped types, or "
                "providers/regions the pricesheet doesn't cover, e.g. Azure/GCP) "
                "— plus any usage-driven cost the priced figures obviously omit "
                "(data egress, request volumes, cross-AZ). Do NOT re-estimate or "
                "restate resources that are ALREADY priced deterministically — "
                "those numbers are authoritative and shown separately. One entry "
                "per resource you can put a number on; omit ones you genuinely "
                "cannot estimate. Empty array is fine if nothing is estimable."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["address", "monthly_min", "monthly_max", "basis"],
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Terraform address of the resource being estimated.",
                    },
                    "type": {"type": "string", "description": "Terraform resource type."},
                    "monthly_min": {"type": "number", "description": "Low end, monthly, USD."},
                    "monthly_max": {"type": "number", "description": "High end, monthly, USD."},
                    "basis": {
                        "type": "string",
                        "maxLength": 400,
                        "description": (
                            "One line on how you arrived at the estimate + the "
                            "key assumption (region, tier, usage), so the reader "
                            "can judge it. This is an estimate, not a quote."
                        ),
                    },
                },
            },
        },
        "narrative": {
            "type": "string",
            "description": (
                "OPTIONAL, secondary. A brief plain-language read of the overall "
                "cost picture — up to ~200 words, resources by terraform address. "
                "Do NOT restate the authoritative deterministic total as if you "
                "computed it (it is given + shown separately). Empty string is "
                "fine; the estimates above are the point, not this prose."
            ),
        },
        "advisories": {
            "type": "array",
            "description": (
                "Optional cost-savings suggestions, ordered biggest-impact "
                "first. Empty array is fine when nothing obvious applies. Each "
                "monthly_saving is YOUR ESTIMATE, not a computed figure."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "title", "detail"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "savings_plan",
                            "reserved",
                            "spot",
                            "rightsizing",
                            "other",
                        ],
                    },
                    "title": {"type": "string", "maxLength": 120},
                    "detail": {"type": "string", "maxLength": 600},
                    "monthly_saving_min": {
                        "type": "number",
                        "description": "Estimated low end of monthly saving (same currency).",
                    },
                    "monthly_saving_max": {
                        "type": "number",
                        "description": "Estimated high end of monthly saving (same currency).",
                    },
                },
            },
        },
    },
}


COST_SUMMARY_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "submit_cost_summary",
        "description": (
            "Submit your cost estimates. Call this tool exactly once. The "
            "primary field is estimated_resources — YOUR estimate for what the "
            "deterministic engine could not price. Never restate an "
            "already-priced figure as your own computation."
        ),
        "parameters": COST_SUMMARY_JSON_SCHEMA,
    },
}


def tool_for_kind(kind: str) -> dict:
    """Return the LiteLLM tool definition matching this summariser kind."""
    if kind == "plan_summary":
        return PLAN_SUMMARY_TOOL
    if kind == "failure_analysis":
        return FAILURE_ANALYSIS_TOOL
    if kind == "cost_summary":
        return COST_SUMMARY_TOOL
    raise ValueError(f"unknown summariser kind: {kind!r}")


PLAN_SUMMARY_SKILL_PROMPT = """\
You are a senior site reliability engineer reviewing a terraform/tofu plan
before it is applied to your employer's production estate. You are
accountable on two fronts, and a good review serves both:

  • Protecting the business — its uptime and its customers' data. An
    outage, a data loss, or an exposure that you wave through is on you.
  • Not crying wolf — teams ship many safe changes a day. Flagging
    routine, low-consequence work as dangerous trains operators to
    ignore your reviews, which is its own failure.

You receive the proposed changes from the plan and the HCL that produced
them. Explain what the plan changes and rate its risk with that dual
responsibility in mind. You are reviewing this change — not redesigning the
wider system — but you also weigh the design quality of the resources this
change itself creates or modifies, across security, reliability, cost, and
operations, grounded in the deterministic signals below (see "Grounded design
review").

You will receive these inputs in the user message:
  • PLAN_JSON — `tofu show -json` output for the proposed changes.
    No-op resource_changes and prior_state have been stripped before
    you see this; everything in `resource_changes` is a real change.
  • CODE_DIFF — unified diff of *.tf / *.tfvars vs the previously-applied
    config (may be absent). Background only: it explains WHY a change
    happens, never WHAT changes or its risk — see the grounding rule below.
  • CODE_CONTEXT — concatenated *.tf source for this run, to look up
    declarations referenced by resource_changes (may be absent).
    Background only, like CODE_DIFF.
  • FLEET_CONTEXT — deployment-wide notes from the operator. May be empty.
  • WORKSPACE_CONTEXT — workspace-specific notes. May be empty.
  • DRIFT_DETECTION (when set) — flags that this run is a scheduled
    drift-detection check, not a response to a configuration change.
    It changes how you frame the whole summary — see the drift-detection
    rule below.
  • SECURITY_FINDINGS (when present) — the deterministic Checkov/Trivy
    security scan for this plan: authoritative, already-computed findings
    (rule id, severity, resource). Ground truth for the security dimension.
  • COST_ESTIMATE (when present) — the deterministic per-resource monthly
    cost estimate for this plan. Ground truth for the cost dimension.

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
  not the diff. A resource whose `actions` is no-op is unchanged for
  this workspace — never describe it, no matter what CODE_DIFF shows.

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

CRITICAL — a drift-detection run is a DETECTION report, not a proposal:

  When DRIFT_DETECTION is set in the user message, this run was queued
  automatically by Terrapod's scheduled drift checker — NOT by a code
  change, a pull request, or an operator. It is plan-only; no apply
  will follow. Its sole purpose is to report whether live
  infrastructure has drifted from its recorded state. Frame the ENTIRE
  summary that way:

    • `resource_changes` here are NOT proposed feature changes. The
      configuration did not change (CODE_DIFF is normally empty). Each
      entry is the corrective action a future reconciling apply WOULD
      take to undo an out-of-band change — i.e. it is describing
      DRIFT. Report it as a finding about what changed in the live
      world: "`aws_s3_bucket.logs` has drifted — bucket versioning was
      disabled out-of-band; the recorded configuration expects it
      enabled." Do NOT phrase it as "this plan will disable
      versioning" — the run is detecting the drift, not causing it.
    • Lead `description` with WHAT drifted and the likely nature of the
      out-of-band change (manual console edit, another tool, an
      external controller). If nothing drifted (no informative
      `resource_changes`), say so plainly — "No drift detected; live
      infrastructure matches recorded state." That is the success case
      for a drift run, not an empty answer.
    • `risk_factors` rate the operational significance of the drift
      (is recorded state now wrong? has a security control been
      silently disabled? would the next real apply revert a needed
      manual fix?), not the routineness of a config change.
    • Never call a drift-detection run "speculative", a "proposed
      change", or something "for review/merge" — there is no proposal
      and no PR. It is a scheduled health check that found (or did not
      find) drift.

CRITICAL — risk is grounded in PLAN_JSON, never in CODE_DIFF:

  Rate `risk_level` SOLELY from the changes in PLAN_JSON — its
  `resource_changes` plus the `resource_drift` reversions described
  above. PLAN_JSON is terraform's authoritative statement of what THIS
  workspace actually changes: it has already resolved which var-files
  load, rendered every template, and evaluated every module. CODE_DIFF
  and CODE_CONTEXT are background to help you EXPLAIN those changes —
  they must NEVER raise `risk_level` above what PLAN_JSON's changes
  justify.

  In particular: if PLAN_JSON has no informative `resource_changes`
  (and no `resource_drift`), `risk_level` is `low` and `risk_factors`
  is `[]` — even when CODE_DIFF is large. A source change that produces
  no planned change for THIS workspace is not a risk to it. This is the
  common shared-monorepo case: one repo holds many environments, a
  change edits one environment's var-file, and every OTHER environment's
  workspace sees that edit in CODE_DIFF but plans zero changes. Their
  risk is `low`. Do not let an edit you can see in the diff, but which
  the plan did not act on, drive the rating.

CRITICAL — `risk_level` and `risk_factors` are paired, not independent:

  An elevated `risk_level` (medium/high/critical) REQUIRES at least one
  `risk_factor`; an empty array is valid ONLY at `low`. Decide the
  severity from what the plan does, then ENUMERATE the factors that
  justify it — if you cannot name even one, it is not elevated: use
  `low` with `[]`. A severity rating with no reasons attached is worse
  than no rating at all.

  A concrete factor names a thing in the plan and why it matters, e.g.:
    {"severity": "medium", "title": "RDS engine_version 16.11 → 16.13",
     "detail": "Aurora minor-version upgrade applies immediately on
     apply (apply_immediately=true); expect a brief connection drop
     while the writer restarts.",
     "resource_address": "module.app_rds[0].aws_rds_cluster.this[0]"}

  Order `risk_factors` worst-first. Severities on each factor match
  the schema enum; the overall `risk_level` should equal the highest
  factor severity.

  Give each materially-distinct risk its OWN `risk_factor`, attributed to
  the specific resource via `resource_address`. Do not fold two distinct
  risks into one entry — an exposed database and the firewall rule that
  exposes it are two factors, each on its own resource, because the
  operator must be able to see and fix each independently. Conversely, do
  not split one risk across several factors. Whenever a factor is about a
  specific resource, set its `resource_address` to that resource's
  terraform address.

How to rate severity:

  Judge the STATE the change leaves the world in, not whether it adds or
  removes. Weigh four dimensions; the worst one present sets `risk_level`:

    • Data loss — could this destroy, replace, or make unrecoverable a
      data store, state, or a recovery point (a database, a volume, a
      bucket with contents, a snapshot)? Unmitigated, that is at least
      high; irreversible loss of production-critical data is critical.
    • Exposure — does it widen who can reach data or actions: a public
      endpoint, ingress opened to the world, broadened IAM/permissions,
      encryption disabled, a secret in plaintext, a privileged workload?
      Provisioning or opening an exposure is a real risk EVEN ON A
      BRAND-NEW resource.
    • Irreversibility — once applied, can it be undone, or is it a
      one-way door (deleting the only backup/snapshot, scheduling key
      destruction, dropping data with no final snapshot)? One-way
      destructive change to something production-critical is critical.
    • Blast radius — how much rides on it: a replace (destroy-then-create
      under a new identity), a mass change across many resources, a
      cross-workspace dependency, a region or provider move.

  A create is NOT automatically low. Standing up a resource that is
  public, unencrypted, over-permissioned, or otherwise exposed carries
  the exposure dimension above and is rated accordingly. `low` is for
  genuinely safe changes: additive, well-scoped, reversible, no exposure
  (a log group, a tightly-scoped role, a tag-only edit).

  Equally, do NOT over-rate — judge by CONSEQUENCE, not by how alarming
  the action verb looks. Crying wolf on routine work is a failure too:

    • A destroy or replace is only as serious as what is actually lost.
      Destroying or replacing a resource that holds no data and gates no
      access has little blast radius — that is low, even though "destroy"
      sounds severe.
    • A change that REDUCES exposure or risk — narrowing who can reach
      something, enabling encryption, adding a protection, removing a
      permission — is an improvement. Rate it low, and do NOT list an
      improvement as a `risk_factor`.
    • Routine, reversible operational changes (rotating a generated value,
      adjusting a non-production knob) are low.

  None of this lowers the bar for real exposure or data loss above — it
  only stops alarm-by-keyword on changes whose consequence is small.

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

Grounded design review (in addition to change risk):

  When SECURITY_FINDINGS and/or COST_ESTIMATE are present, extend your
  review beyond the change's blast radius to the design quality of the
  resources this plan creates or modifies — as ADDITIONAL `risk_factors`,
  each tagged with the matching `category`:
    • security — driven by SECURITY_FINDINGS. The scanner is ground truth:
      surface and prioritise its material findings in context (exposure,
      over-broad IAM, missing encryption, public endpoints), anchored to the
      resource address. Do NOT re-list every rule mechanically, and do NOT
      invent security issues the scanner did not report.
    • cost — driven by COST_ESTIMATE. Its figures are ground truth: flag
      obviously oversized / always-on resources or a cost this change adds
      that looks disproportionate, and cite the monthly figure.
    • reliability — SPOFs, single-AZ, no replica/backup/PITR, fragile
      dependencies among the resources this change stands up.
    • operations — missing tags/labels, no lifecycle, poor rollback story.

  These design factors are ADDITIVE to the change-risk factors; the change
  risks themselves keep `category: change` or omit the category. `risk_level`
  remains the single overall grade — raise it only for a genuine, consequential
  problem, never for a scanner nit on a throwaway fixture (weigh FLEET_CONTEXT,
  same "don't cry wolf" bar). If SECURITY_FINDINGS and COST_ESTIMATE are both
  absent, review the change exactly as before and add nothing.

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
You are an infrastructure engineer on call, debugging a terraform/tofu run
that just failed — in `plan` or in `apply`. A colleague is blocked and
waiting on you. You have the run log for the failed phase and the HCL that
was being processed. Find the root cause and give concrete, ordered fixes a
colleague could act on immediately. Nothing else — you are debugging this
failure, not reviewing the whole codebase.

You will receive these inputs in the user message:
  • PLAN_LOG or APPLY_LOG — the terraform/tofu stdout+stderr leading
    up to the failure. The label tells you which phase failed. May be
    truncated from the head; the tail (where the error typically
    appears) is preserved.
  • CODE_DIFF — unified diff of *.tf / *.tfvars between this run's
    configuration and the previously-applied configuration. May be
    absent. When present, suspect it as a potential cause — recent
    HCL changes are the most likely culprit for new failures.
  • CODE_CONTEXT — concatenated .tf source. May be absent.
  • FLEET_CONTEXT — deployment-wide notes. May be empty.
  • WORKSPACE_CONTEXT — workspace-specific notes. May be empty.
  • STATE_DIVERGED (apply-phase only, when set) — flags that the
    runner couldn't upload the post-apply state to Terrapod. Real
    infrastructure may have been mutated by the partial apply but
    Terrapod's recorded state no longer matches. Call this gap out
    explicitly in `description` and weight remediation accordingly.

When the label is APPLY_LOG, this is an APPLY-PHASE failure — extra
rules apply:
  • Identify the specific resource whose Create/Modify/Destroy
    failed. Apply output names it directly ("Error: ... with
    aws_instance.foo, on main.tf line N").
  • Identify which resources had ALREADY completed before the
    failure. Apply prints "Creation complete after Ns" / "Destruction
    complete after Ns" / "Modifications complete after Ns" per
    resource as it goes. Anything between the start of the apply and
    the first "Error:" succeeded; anything in flight or after did not.
  • The infrastructure is now in a PARTIAL state. State on the
    Terrapod side should still reflect what actually completed (state
    is written after each resource), but the operator needs to know
    the gap explicitly. Mention it in `description`.
  • Rank fixes by whether the apply is safe to re-run as-is, requires
    a `terraform refresh` first, requires manual cleanup of orphaned
    resources, or requires a targeted re-apply (`-target=...`).

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
    state_diverged: bool = False,
    drift_detection: bool = False,
    security_findings: str = "",
    cost_estimate: str = "",
    output_language: str = "",
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
    if output_language.strip() and output_language.strip().lower() != "english":
        # Canonical-language directive (#767): the deployment writes summaries in
        # one authoritative language. Identifiers stay verbatim so the output is
        # still actionable; only the natural-language prose is in this language.
        parts.append(
            f"OUTPUT_LANGUAGE: Write every natural-language field you emit — the "
            f"description and each risk factor's title and detail — in "
            f"{output_language.strip()}. Keep all identifiers verbatim and "
            f"untranslated: resource addresses, provider and module names, HCL "
            f"keywords, CLI flags, file paths, and anything inside backticks."
        )
    system_message = "\n\n".join(parts)

    user_parts: list[str] = []
    if fleet_context.strip():
        user_parts.append(f"FLEET_CONTEXT:\n{fleet_context.strip()}")
    if workspace_context.strip():
        user_parts.append(f"WORKSPACE_CONTEXT:\n{workspace_context.strip()}")

    if state_diverged and kind == "failure_analysis":
        user_parts.append(
            "STATE_DIVERGED: true\n"
            "(the runner could not upload post-apply state to Terrapod; "
            "real infrastructure may have been mutated by the partial "
            "apply but Terrapod's recorded state no longer reflects it.)"
        )
    if drift_detection and kind == "plan_summary":
        user_parts.append(
            "DRIFT_DETECTION: true\n"
            "(scheduled drift-detection run — plan-only, no apply will "
            "follow. resource_changes describe drift between recorded "
            "state and live infrastructure, not proposed configuration "
            "changes. Frame the summary as drift findings.)"
        )
    user_parts.append(f"{primary_input_label}:\n```{primary_input_lang}\n{primary_input}\n```")
    if code_diff.strip():
        user_parts.append(f"CODE_DIFF:\n```diff\n{code_diff}\n```")
    if code_context_truncated.strip():
        user_parts.append(f"CODE_CONTEXT:\n```hcl\n{code_context_truncated}\n```")
    # Grounded design-review signals (secondary, ground-truth context). Only
    # present for plan_summary runs where the scan / cost artifacts exist.
    if security_findings.strip():
        user_parts.append(f"SECURITY_FINDINGS:\n```json\n{security_findings}\n```")
    if cost_estimate.strip():
        user_parts.append(f"COST_ESTIMATE:\n```json\n{cost_estimate}\n```")

    tool_name = "submit_plan_summary" if kind == "plan_summary" else "submit_failure_analysis"
    user_parts.append(f"Now call the `{tool_name}` tool exactly once with your structured answer.")

    user_message = "\n\n".join(user_parts)
    return system_message, user_message


# --- Cost summary prompt (#871) ---------------------------------------------
COST_SUMMARY_SKILL_PROMPT = """\
You are a senior cloud FinOps engineer. A deterministic pricing engine \
has ALREADY priced the resources it has data for; those \
figures are authoritative and shown to the user separately. Some resources it \
could NOT price — they are listed under UNPRICED (unmapped types, or \
providers/regions its pricesheet doesn't cover, e.g. Azure/GCP).

YOUR PRIMARY JOB is to fill that gap: from your own cost knowledge, ESTIMATE the \
monthly cost of the unpriced resources — that is the whole point of this task, \
not writing prose.

Hard rules:
- estimated_resources is the main output. Estimate monthly cost ONLY for the \
UNPRICED resources (and any obviously-omitted usage-driven cost — egress, \
requests, cross-AZ). NEVER re-estimate or restate a resource that is already \
priced deterministically; those numbers are authoritative and yours would only \
add noise. Give each estimate a one-line `basis` stating your key assumption \
(region, tier, usage) so the reader can judge it. Omit anything you genuinely \
cannot estimate; an empty array is fine if nothing is estimable.
- Everything you output is an ESTIMATE (the API tags it `source: ai-estimate`) \
— it is shown as a separate overlay, never merged into the authoritative total, \
never a gate. Do not present a different authoritative total.
- Advisories (savings) are secondary and specific to what is present (long-lived \
on-demand → Savings Plan/RI; fault-tolerant batch → spot; oversized → \
rightsizing). Their monthly_saving is also your estimate.
- narrative is optional and secondary — a brief read of the picture, or empty. \
Refer to resources by terraform address. No chain-of-thought, no preamble.\
"""


def render_cost_prompt(
    *,
    estimate_json: str,
    plan_json: str = "",
    fleet_context: str = "",
    workspace_context: str = "",
    prompt_prefix: str = "",
    prompt_suffix: str = "",
    output_language: str = "",
) -> tuple[str, str]:
    """Render the (system, user) messages for the cost-summary request.

    ``estimate_json`` is the authoritative deterministic cost estimate (the
    ``cost_estimate.json`` artifact body). ``plan_json`` is optional extra
    context (the structured plan) — pass "" to omit it. The output contract is
    the ``submit_cost_summary`` tool; provenance guardrails live in the skill
    prompt above and are re-asserted server-side (advisories are stamped
    ``source: "ai-estimate"`` regardless of what the model returns).
    """
    parts: list[str] = []
    if prompt_prefix.strip():
        parts.append(prompt_prefix.strip())
    parts.append(COST_SUMMARY_SKILL_PROMPT)
    if prompt_suffix.strip():
        parts.append(prompt_suffix.strip())
    if output_language.strip() and output_language.strip().lower() != "english":
        parts.append(
            f"OUTPUT_LANGUAGE: Write every natural-language field you emit — the "
            f"narrative and each advisory's title and detail — in "
            f"{output_language.strip()}. Keep all identifiers verbatim and "
            f"untranslated: resource addresses, provider and module names, HCL "
            f"keywords, CLI flags, file paths, currency codes, and anything "
            f"inside backticks."
        )
    system_message = "\n\n".join(parts)

    user_parts: list[str] = []
    if fleet_context.strip():
        user_parts.append(f"FLEET_CONTEXT:\n{fleet_context.strip()}")
    if workspace_context.strip():
        user_parts.append(f"WORKSPACE_CONTEXT:\n{workspace_context.strip()}")
    user_parts.append(
        "DETERMINISTIC_COST_ESTIMATE (authoritative — priced resources are in "
        "`resources`; the ones the engine could NOT price are in `unpriced`, and "
        "THOSE are what you estimate). A priced resource may carry "
        "`usage_assumptions`: usage-driven line items (requests, data processed, "
        "duration) priced at a TYPICAL guess, each with a low/typical/high "
        "quantity band AND its monthly dollar impact at each point "
        "(`cost_low`/`cost_typical`/`cost_high`) — so its real cost can swing "
        "across `cost_low`–`cost_high` with traffic, and the priced figure folds "
        "in only `cost_typical`:\n"
        f"```json\n{estimate_json}\n```"
    )
    if plan_json.strip():
        user_parts.append(f"PLAN_JSON (context):\n```json\n{plan_json}\n```")
    user_parts.append(
        "Now call the `submit_cost_summary` tool exactly once: estimate the "
        "UNPRICED resources in `estimated_resources` (the primary output), then "
        "any savings advisories, then an optional brief narrative. Where a priced "
        "resource carries `usage_assumptions`, use them: in the narrative/"
        "advisories, flag the usage-driven items that dominate its cost, and — "
        "only where the resource's own context (name, config, workspace) clearly "
        "suggests it — say whether its real usage sits toward the low or high end "
        "of the band. Do not restate the deterministic figures as your own."
    )

    user_message = "\n\n".join(user_parts)
    return system_message, user_message
