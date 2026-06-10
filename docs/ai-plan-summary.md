# AI Plan Summary

Terrapod can attach an AI-generated change summary and risk assessment
to every plan, and an AI-generated failure analysis to every plan that
errors. The summary lands in the run UI alongside the plan output, can
be edited into the PR/MR comment for VCS-driven runs, and is queryable
via the API.

The feature is provider-agnostic. Terrapod uses the
[LiteLLM](https://github.com/BerriAI/litellm) Python library
in-process, so a single configuration block reaches every major model
catalogue: AWS Bedrock (Anthropic Claude, Amazon Nova, Mistral, Meta
Llama, OpenAI gpt-oss…), OpenAI direct (GPT-5, o-series),
Anthropic direct (Claude Opus/Sonnet/Haiku), Google AI Studio / Vertex
(Gemini), Azure OpenAI, vLLM, OpenRouter, and any other OpenAI-compat
endpoint.

The feature is **off by default**. Enabling it requires both flipping
`ai_summary.enabled: true` and choosing a model.

## What you get

For a plan that succeeds (`status=planned`):

- A change description (~600 words max, markdown-formatted) referring
  to resources by their terraform address
- An overall `risk_level` — one of `low`, `medium`, `high`, `critical`
- A list of `risk_factors` ordered worst first, each with a severity,
  title, detail, and (optionally) the affected resource address

For a run that errored during EITHER plan or apply (`errored`):

- A description of the root cause in operator terms. For apply-phase
  failures the description also identifies the specific resource
  whose Create/Modify/Destroy failed, calls out which resources had
  already completed before the failure (the infrastructure is in a
  partial state), and flags the state gap when the workspace is
  marked `state_diverged` (the runner couldn't upload the post-apply
  state).
- A severity rating for how blocking the failure is.
- A list of suggested fixes with concrete steps. For apply-phase
  failures these are ranked by recovery type: re-run safe vs.
  needs `terraform refresh` vs. needs manual cleanup vs. needs a
  targeted re-apply (`-target=...`).

Both kinds are persisted in the `plan_summaries` table, returned by
`GET /api/v2/plans/{plan-id}/summary`, and announced over the
per-workspace SSE channel as a `plan_summary_ready` event so the UI
re-fetches without polling.

## Quick start

### AWS Bedrock + Anthropic Claude Opus

```yaml
api:
  config:
    ai_summary:
      enabled: true
      # Bedrock requires the inference-profile ID (not the bare
      # foundation-model ID) for on-demand invocation of newer models.
      model: bedrock/us.anthropic.claude-opus-4-8
      max_output_tokens: 1024
      daily_token_budget: 1000000   # cap output tokens per UTC day
      auth:
        aws_region: us-east-1
        # Optional cross-account hop. When set, Terrapod's pod-side
        # IAM identity (IRSA) calls sts:AssumeRole before invoking
        # Bedrock. Empty = use the pod's ambient credentials.
        aws_role_arn: arn:aws:iam::703581221739:role/terrapod
      context:
        fleet_context: |
          Production AWS infrastructure for service-X.
          Workspaces are sharded by region (us-east-1, eu-west-1).
          Treat IAM role/policy churn on the `runner-*` set as critical;
          treat S3 lifecycle adjustments as low-risk unless the bucket
          name contains `audit`.
```

IAM policy on the `terrapod` role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeAnyBedrockModel",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*:ACCOUNT:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:::foundation-model/*"
      ]
    }
  ]
}
```

The third resource (regionless `foundation-model/*`) is required when
the `global.*` cross-region inference profiles are used — those fan
out to a foundation model whose ARN has no region.

Trust policy: trust the API pod's IRSA role via the
`aws:PrincipalArn` ArnEquals condition.

### OpenAI direct

```yaml
api:
  config:
    ai_summary:
      enabled: true
      model: openai/gpt-5
      auth:
        # Inject via env var rather than committing to values.yaml.
        # Set TERRAPOD_AI_SUMMARY__AUTH__API_KEY from a K8s Secret.
        # api_key: ""
```

K8s Secret + env var:

```yaml
api:
  extraEnvFrom:
    - secretRef:
        name: terrapod-ai-summary-openai
```

```sh
kubectl -n terrapod create secret generic terrapod-ai-summary-openai \
  --from-literal=TERRAPOD_AI_SUMMARY__AUTH__API_KEY=sk-...
```

### Anthropic direct

Same shape as OpenAI direct, with `model: anthropic/claude-opus-4-8`.
The Secret carries the Anthropic API key under the same env var name.

### Google Gemini (AI Studio)

```yaml
api:
  config:
    ai_summary:
      enabled: true
      model: gemini/gemini-2.5-pro
```

Secret carries the Google AI Studio API key under
`TERRAPOD_AI_SUMMARY__AUTH__API_KEY`.

### Self-hosted vLLM / OpenAI-compat gateway

```yaml
api:
  config:
    ai_summary:
      enabled: true
      model: openai/llama-3.3-70b
      api_base: https://vllm.your-cluster.svc.cluster.local/v1
      # api_key still required by the SDK; can be a placeholder value
      # when the upstream doesn't actually check it.
```

## Provider matrix

| Provider | `model` prefix | Auth source |
|---|---|---|
| AWS Bedrock | `bedrock/...` | boto3 chain: IRSA → optional `sts:AssumeRole` (via `aws_role_arn`) |
| OpenAI | `openai/...` | `auth.api_key` |
| Anthropic | `anthropic/...` | `auth.api_key` |
| Google AI Studio | `gemini/...` | `auth.api_key` |
| Azure OpenAI | `azure/<deployment>` | `auth.api_key` + `api_base` |
| Vertex AI | `vertex_ai/...` | GCP ADC (workload identity) |
| vLLM, OpenRouter, Together, Groq, … | `openai/<model>` | `auth.api_key` + `api_base` |

`api_base` is only needed when the upstream isn't the vendor default
(self-hosted, gateway, pinned Azure resource, etc.).

## Workspace-level controls

Each workspace has two opt-in fields:

- `ai_summary_mode` — one of `default` (follow the deployment-wide
  switch), `enabled` (always summarise this workspace), or `disabled`
  (never summarise this workspace, regardless of global setting).
- `ai_summary_context` — free text up to 4000 characters, appended to
  the prompt as workspace-specific facts. Use this to surface
  blast-radius warnings ("destroying the KMS key in this workspace
  causes a global outage") or domain knowledge.

Set both via the workspace settings UI, the API (`PATCH
/api/v2/workspaces/{id}`), the `terrapod_workspace` Terraform resource
attributes (`ai_summary_mode` and `ai_summary_context`), or the bulk
update endpoint.

## Prompt customisation

The model's system prompt is composed of four layers, top to bottom:

1. **In-code skill prompt** — defines the task, the JSON output
   schema, and the guardrails. Owned by Terrapod source; operators
   cannot override (changing it would break the DB schema and the UI
   contract).
2. **`ai_summary.context.prompt_prefix`** — free-text prepended before
   the skill prompt. Use for tone/emphasis tweaks ("be terse", "lead
   with destroys"). Do NOT use this to change the output contract.
3. **`ai_summary.context.fleet_context`** — deployment-wide facts
   about your infrastructure (what runs here, naming conventions,
   provider/action pairs to flag).
4. **`ai_summary.context.prompt_suffix`** — same as prefix, appended.
5. (in the user message, after the system prompt) **Per-workspace
   `ai_summary_context`** column — additive to fleet context.

Tone/emphasis tweaks are safe in prefix/suffix; output schema changes
are not. The DB schema and SSE/UI contract assume the skill prompt's
JSON shape.

## Daily token budget

`ai_summary.daily_token_budget` caps total output tokens spent across
all summaries per UTC day. Tracked in Redis. Once exhausted, further
plans are recorded with `status='skipped'` and `error_message='daily
token budget exhausted'`; the budget resets at the next UTC midnight.
Set to `0` for unlimited.

The counter is on output tokens only (cheaper to reason about than
mixed input/output cost). Input-side cost is bounded by
`plan_json_max_bytes` (default 500 KB) and `code_context_max_bytes`
(default 200 KB).

## Skipping and overrides

A plan **does NOT** get a summary when:

- The feature is globally disabled (`ai_summary.enabled: false`)
- The workspace's `ai_summary_mode` is `disabled`
- The daily token budget has been exhausted
- The plan has no JSON output (older runs, or runner upload failed)

In every case, run lifecycle is unaffected — the feature is best-effort
and never blocks plan or apply.

## What gets sent to the model

A full audit of the assembled prompt for both `plan_summary` and
`failure_analysis` kinds — and, by extension, every follow-up chat
turn, which reuses the same prefix (#463).

**System message** (the cacheable prefix's first block):
- The skill prompt (`PLAN_SUMMARY_SKILL_PROMPT` or
  `FAILURE_ANALYSIS_SKILL_PROMPT` from `summariser_prompt.py`) —
  describes the task, output schema, and tone.
- `ai_summary.context.prompt_prefix` (operator-set, optional) —
  prepended to the skill prompt.
- `ai_summary.context.prompt_suffix` (operator-set, optional) —
  appended.

**Initial user message** (the cacheable prefix's second block; this is
the bulk of the request):
- `FLEET_CONTEXT` — `ai_summary.context.fleet_context` from Helm
  values. Deployment-wide facts (free-form prose).
- `WORKSPACE_CONTEXT` — the workspace's `ai_summary_context` column
  (set via the workspace settings UI). Per-workspace facts.
- `STATE_DIVERGED` block — only on `failure_analysis` runs where the
  workspace is flagged `state_diverged`. Tells the model real
  infrastructure may have been mutated by a partial apply but
  Terrapod's recorded state no longer reflects it.
- `PLAN_JSON` (for `plan_summary`) — the structured plan JSON
  output by the runner, cleaned (`prior_state` stripped, no-op
  resource_changes pruned, drift partitioned, etc. — see
  `_clean_plan_json_bytes`). Capped at
  `ai_summary.plan_json_max_bytes` (default 500 KB).
- `PLAN_LOG` / `APPLY_LOG` (for `failure_analysis`) — the raw text
  of the run log. Tail-truncated when over `plan_json_max_bytes`.
- `CODE_DIFF` — unified `git diff --no-index` of `*.tf` / `*.tfvars`
  between this run's config version and the previously-applied
  config version. Empty when there's no prior CV (first run, or
  GC'd). Capped at `ai_summary.code_diff_max_bytes` (default 100 KB).
- `CODE_CONTEXT` — concatenated `.tf` files from this run's
  config-version tarball. Capped at
  `ai_summary.code_context_max_bytes` (default 200 KB).
- Tool-call instruction (initial-summary path only) — "Now call
  the `submit_plan_summary` tool exactly once with your structured
  answer."

**Follow-up turns** (chat) — appended AFTER the cacheable prefix:
- The assistant's initial structured summary as plain text (just
  the `description` — risk factors are NOT replayed in chat
  history to keep token cost down).
- Every prior user / assistant turn from `plan_summary_messages`,
  in chronological order.
- The new user turn.

### What is NEVER sent

- **State files.** Neither current state nor any historical state
  version. `_gather_inputs` reads from the config-version tarball
  (HCL source) and the plan JSON (which has its embedded
  `prior_state` snapshot stripped before it leaves the API).
  `TestNoStateLeakage` in `tests/services/test_summariser.py` pins
  this invariant by introspection — the module physically does not
  import `StateVersion` or any state-key helper. The same invariant
  holds on the chat path: it reuses the same `_gather_inputs` +
  `render_prompt` flow.
- **Sensitive workspace variables.** Workspace variables and
  variable-set variables are injected at runtime into the runner
  Job; the summariser code path never reads from the `variables`
  table. `sensitive=True` values are also masked in API responses
  and not stored in plaintext logs.
- **Secrets in any other table.** The summariser only touches
  `runs`, `workspaces`, `plan_summaries`, `plan_summary_messages`,
  and object storage at `config/*` (CV tarball) + `plans/*` (plan
  log / plan JSON / plan-artifacts).
- **API tokens, runner tokens, session tokens.** None of the auth
  surfaces feed the summariser inputs.

If you customise the prompt via `prompt_prefix`, `prompt_suffix`,
`fleet_context`, or per-workspace `ai_summary_context`, anything
you put in those fields **does** reach the model. Don't paste
secrets into them.

## How it works under the hood

1. A run reaches a terminal state (`planned` → `plan_summary` kind,
   or `errored` at any point → `failure_analysis` kind, plan-phase or
   apply-phase).
2. The API enqueues an `ai_plan_summary` trigger via the distributed
   scheduler (multi-replica safe; deduped per `(run_id, kind)`).
3. Any API replica picks up the trigger, gathers the inputs:
   - `plan_summary` reads the structured plan JSON.
   - `failure_analysis` reads the **apply log** when
     `apply_started_at` is set, otherwise the **plan log**. When the
     workspace's `state_diverged` flag is true, a `STATE_DIVERGED`
     block is included in the prompt so the model flags the
     state-vs-reality gap explicitly.
   - Both kinds also include the configuration version's `.tf` source
     for code context and a unified diff against the previously-
     applied configuration when one is available.
   Renders the prompt with the layered context and calls
   `litellm.acompletion`.
4. The structured JSON response is parsed and upserted into the
   `plan_summaries` table.
5. A `plan_summary_ready` SSE event is published on the per-workspace
   channel; the run-detail UI re-fetches.
6. (For VCS-driven runs, future work) The summary is edited into the
   PR/MR comment in place per head SHA.

The handler is registered as a distributed task and only runs when
`ai_summary.enabled` is true. Disabling the feature drops the trigger
registration on the next API restart.

## Troubleshooting

**`status=errored, error_message=...`** — the model call failed. Common
causes:

- IAM policy missing required Bedrock actions or model resources
- Anthropic models invoked on Bedrock's OpenAI-compat surface
  (`bedrock-mantle`) — Bedrock returns
  `"does not support the '/v1/chat/completions' API"`. Terrapod's
  default Bedrock path uses Converse, which **does** support Claude;
  this only happens if you've manually configured a different
  endpoint
- Bedrock's "Invocation of model ID X with on-demand throughput isn't
  supported" — switch the model to its inference-profile form (e.g.
  `bedrock/us.anthropic.claude-opus-4-8` instead of
  `bedrock/anthropic.claude-opus-4-8`)
- API key invalid or revoked
- Network: the API pod cannot reach the upstream — check egress
  policies / private endpoints

**`status=skipped`** — see the `error_message` field on the row; it
explains whether the workspace opted out or the daily budget was hit.

**No row appears at all (404 from `GET /plans/.../summary`)** — the
feature is globally disabled or the handler hasn't run yet. Check the
API logs for `Registered trigger handler` (`ai_plan_summary`).

**Wrong-looking risk assessment** — tune `fleet_context` to flag
blast-radius concerns specific to your deployment. Add workspace-level
`ai_summary_context` for the specific workspace.

**Cost runaway** — set `daily_token_budget` to a fixed ceiling.
Telemetry (input + output tokens per summary) is recorded on every
row.

## Follow-up chat (#463)

Once the initial summary lands, the run-detail panel shows a chat
input. Operators can ask clarifying questions ("you say my RDS
instance will be updated in place, how long will that take?") and
the model answers grounded in the same plan context.

**One shared thread per run.** Anyone with workspace read can see
and post in the thread — modeled on GitHub Copilot's per-PR
conversation, not per-user. Closing the tab doesn't end the thread.

**Same hard constraints as the initial summary.** No state file is
ever sent to the AI. No tool access (text-in / text-out only). The
follow-up prompt reuses the byte-identical system + initial-user
prefix the initial summary used, so prompt-caching providers serve
the heavyweight plan-context prefix from cache — a follow-up turn
typically costs ~10% the input-token price of a fresh request.

**Bounds (Helm-configurable):**

| Setting | Default | Effect |
|---|---|---|
| `ai_summary.followup_max_messages_per_run` | 20 | User-turn cap. Once reached, the UI disables the input. `0` disables chat entirely (initial summary still fires). |
| `ai_summary.followup_max_output_tokens` | 2048 | Per-reply output-token cap. Smaller than `max_output_tokens` because follow-ups are conversational, not full re-summaries. |
| `ai_summary.daily_token_budget` | 0 (unlimited) | Whole conversation contributes to the same daily pool as the initial summary. Once hit, the chat input shows the budget-exhausted banner. |

No on-the-fly summarisation of older turns: every follow-up sends
the full conversation. Per-turn cost stays bounded by the caps
above + the prefix cache hit.

### Choosing a model for chat

Prompt caching matters more for chat than for one-shot summaries.
Providers that cache the prefix amortise the cost across every
follow-up; providers that don't pay full price for the plan
context on each turn.

**Recommended** (caching + chat-tuned):
- **Anthropic Claude** — direct (`anthropic/claude-sonnet-4-6`,
  `anthropic/claude-opus-4-8`) or via Bedrock
  (`bedrock/us.anthropic.claude-sonnet-4-6`).
- **OpenAI GPT-4.x / o-series** — automatic prefix caching past
  ~1024 tokens.
- **Amazon Nova Pro / Lite** on Bedrock — Anthropic-compatible
  cache markers.
- **DeepSeek** direct — automatic prefix caching.

The chart default (`bedrock/us.anthropic.claude-sonnet-4-6`) sits
in this tier — chosen for the Bedrock prompt-caching support,
proven multi-turn coherence, and ~5× lower cost than Opus while
better at the structured-output tool calls used by the initial
summary.

**Usable but suboptimal** (no caching; expect higher cost per turn
AND degraded coherence over long conversations):
- Bedrock Llama / Mistral / Cohere
- Gemini
- Azure OpenAI on older deployments
- Groq
- Self-hosted vLLM / LiteLLM proxy
- OpenRouter (even when proxying a cacheable upstream — the cache
  is keyed on the OpenRouter prefix, not the upstream's)

Detection is by model-string prefix only. Switching providers is a
config change — `ai_summary.model` in Helm values + the matching
auth block.

### Disabling chat

Set `ai_summary.followup_max_messages_per_run: 0` to disable the
chat surface entirely while keeping the initial summary. The
panel still renders the summary; no chat input appears. Existing
threads stay readable.

## Feedback

The AI surface is new; quality reports drive iteration.

Please raise GitHub issues against
[mattrobinsonsre/terrapod](https://github.com/mattrobinsonsre/terrapod/issues)
with any constructive feedback on the AI surface. The most useful
reports include:

- **The model you're using** — the
  `api.config.ai_summary.model` value (e.g.
  `bedrock/us.anthropic.claude-sonnet-4-6`).
- **Any prompt customisations** — per-workspace
  `ai_summary_context`, platform-level `prompt_prefix` /
  `prompt_suffix`. The project can only learn from prompt
  mutations it knows about.
- **Whether the issue is with `plan_summary` or
  `failure_analysis`** — the two kinds have different prompt
  skeletons and different failure modes.
- **The chat exchange that surfaced the issue**, if applicable —
  copy the relevant turns out of the thread (omit anything
  sensitive about your fleet).

No backend submission flow, no anonymisation pipeline, no
aggregation — operators review what they share and submit
manually so privacy stays with them.

## See also

- [Authentication](authentication.md) — how API tokens and runner
  tokens authenticate to Bedrock IAM / IRSA
- [Cloud Credentials](cloud-credentials.md) — IRSA, GCP WIF, Azure WI
  setup for the API pod
- [Notifications](notifications.md) — webhook/Slack/email delivery is
  separate; the summary is included in the run detail link in those
  notifications
