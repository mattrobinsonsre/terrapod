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

## See also

- [Authentication](authentication.md) — how API tokens and runner
  tokens authenticate to Bedrock IAM / IRSA
- [Cloud Credentials](cloud-credentials.md) — IRSA, GCP WIF, Azure WI
  setup for the API pod
- [Notifications](notifications.md) — webhook/Slack/email delivery is
  separate; the summary is included in the run detail link in those
  notifications
