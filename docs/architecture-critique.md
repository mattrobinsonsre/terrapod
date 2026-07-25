# Architecture Critique

> Part of Terrapod's **AI-augmented review layer** design focus — see [Why Terrapod](../README.md#is-terrapod-for-you). Rides the same switch as the [AI plan summary](ai-plan-summary.md); disabled by default, opt in per the configuration below.

Terrapod can attach a **senior cloud-architect AI review** of the infrastructure a run *proposes* — a reasoning layer that reads the plan and calls out design, reliability, cost, security, operations, and scalability concerns in the same way an experienced platform engineer would in a design review, before you apply.

It sits **on top of** the deterministic [IaC security scan](security-scanning.md): the Checkov/Trivy scan is the deterministic, rule-catalogue layer that produces concrete misconfiguration findings and can *gate* a run when enforced; the architecture critique is the AI *reasoning* layer above it that reasons about the change as a whole. The two are distinct — the critique never restates or replaces the scan's verdict, and it **never gates a run**.

The feature is provider-agnostic (via [LiteLLM](https://github.com/BerriAI/litellm)) and reuses the AI plan-summary plumbing wholesale — the same model, provider matrix, daily token budget, and per-workspace controls. There is **no separate config or Helm key**: it rides `api.config.ai_summary.enabled`.

## What it reads (and what it never reads)

The critic's input is the run's **plan JSON `planned_values`** — the same plan-time artifact the deterministic security scan consumes — fetched and sanitised through the plan-summary path's existing helpers (sensitive attribute values are stripped, and the JSON is structurally size-bounded before it leaves the API).

It reads **the plan, not the state**. No Terraform state file — current or historical — is ever sent to the model, and no sensitive workspace variables are read. This is the same hard invariant the plan summary holds, pinned by a source-introspection guardrail test (`TestArchitectureCritiqueNoStateLeakage`) that fails CI if the service ever reaches for a state key.

## What you get

For a run with a usable plan JSON, the critique carries:

- `critique` — a prose narrative: a senior cloud/platform architect's read of the proposed infrastructure, referring to resources by their terraform address.
- `risk_level` — an overall qualitative grade: one of `critical`, `high`, `medium`, `low`, `none`.
- `findings` — a list of structured concerns, each `{severity, category, title, detail, address}`:
  - `severity` ∈ `critical`, `high`, `medium`, `low`, `info`
  - `category` ∈ `security`, `reliability`, `cost`, `operations`, `scalability`, `other`
  - `address` — the affected terraform resource address (optional)

The critique is persisted in the `architecture_critiques` table, returned by `GET /api/terrapod/v1/runs/{run_id}/architecture-critique`, and announced over the per-workspace SSE channel so the UI re-renders without polling.

## Where it shows up

The critique renders in the run page's **Security** tab, **on top of** the deterministic security-scan panel. Like the cost AI layer, the panel self-hides when there is no critique (a `404` from the GET endpoint), so with the AI switch off, the tab shows only the deterministic scan.

The deterministic security-scan GET response advertises the critique's URL as `ai-critique-url` in its `meta` block, so the UI knows where to fetch the reasoning layer for the run.

## Advisory only — it never gates a run

The critique is **advisory reasoning only**. It is generated after the fact, best-effort, off the run's critical path:

- It never blocks, delays, or fails a plan or apply.
- It never changes the run's outcome, lock state, or the deterministic scan's verdict.
- A model outage, a budget exhaustion, or a parse failure records a `skipped`/`errored` row and is otherwise invisible to the run lifecycle.

If you want a stage that can actually *hold* a run on a finding, that is the enforced [security scan](security-scanning.md) (or an [OPA policy set](policies.md)) — not this.

## Enabling it

The critique is **off by default** and shares the AI plan-summary switch — enabling that one switch (plus a model) turns on plan summaries, the cost AI layer, and this critique together.

```yaml
api:
  config:
    ai_summary:
      enabled: true
      model: bedrock/us.anthropic.claude-opus-4-8
      # daily_token_budget, auth, context, etc. — all shared with the
      # plan summary. See docs/ai-plan-summary.md for the full config,
      # provider matrix, and IAM setup.
```

See [AI Plan Summary → Quick start](ai-plan-summary.md#quick-start) for the full provider matrix (Bedrock, OpenAI, Anthropic, Gemini, Azure, vLLM, …), auth blocks, and the daily token budget.

### Double-gating (global switch + per-workspace mode)

Whether a given run gets a critique is decided by **two gates**, exactly as for the plan summary:

1. **Global** — `api.config.ai_summary.enabled` must be `true`.
2. **Per-workspace** — the workspace's `ai_summary_mode`:
   - `default` — follow the deployment-wide switch (the common case).
   - `enabled` — always critique this workspace's runs (when the global switch is on).
   - `disabled` — never critique this workspace, regardless of the global switch.

The scheduler handler re-checks both gates when it runs, so enqueuing a critique while the feature is disabled is a safe no-op — no row is written and no model call is made. The **daily token budget** (`ai_summary.daily_token_budget`, shared across plan summaries, the cost AI layer, and critiques) is the third gate: once exhausted, further critiques are recorded `status='skipped'` until the next UTC midnight.

Per-workspace `ai_summary_context` (free-text workspace facts) is appended to the prompt for the critique the same way it is for the plan summary — use it to hand the architect domain knowledge ("this workspace fronts a public API; flag anything that widens its ingress").

## Follow-up chat

Once the initial critique lands, the panel offers a follow-up chat thread grounded in the same plan context — ask the architect to expand on a finding or weigh an alternative. One shared thread per run (anyone with workspace `read` can see and post, GitHub-PR-conversation semantics), bounded by the same per-run message cap and daily token budget as the [plan-summary chat](ai-plan-summary.md#follow-up-chat-463). No state is ever sent on the chat path either — it reuses the same sanitised plan-JSON prefix.

## API

| Method + path | Purpose |
|---|---|
| `GET /api/terrapod/v1/runs/{run_id}/architecture-critique` | Read the critique for a run (`404` when none — the UI's "no AI surface" signal). |
| `POST /api/terrapod/v1/runs/{run_id}/architecture-critique/regenerate` | Re-fire the critique (workspace `read`; doesn't mutate infrastructure). |
| `GET /api/terrapod/v1/runs/{run_id}/architecture-critique/messages` | The follow-up chat transcript. |
| `POST /api/terrapod/v1/runs/{run_id}/architecture-critique/messages` | Post a question; returns the synchronous assistant reply. |

Full request/response shapes, the JSON:API attribute reference, and the SSE event lifecycle are in the [API reference → Architecture Critique](api-reference.md#architecture-critique-9631036).

## How it works under the hood

1. After a run's plan JSON is uploaded, the API enqueues an `ai_architecture_critique` trigger via the distributed scheduler (multi-replica safe; deduped per run).
2. Any API replica picks up the trigger, re-checks both gates and the budget, fetches and sanitises the plan JSON `planned_values`, renders the senior-architect prompt, and calls the model.
3. The structured JSON response is parsed and upserted into `architecture_critiques` (idempotent on `run_id` — a `ready` row is never overwritten by a later errored attempt).
4. An `architecture_critique_ready` SSE event is published on the per-workspace channel; the run-detail Security tab re-fetches and renders it above the scan panel.

The handler is registered only when `ai_summary.enabled` is true; disabling the feature drops the registration on the next API restart.

## See also

- [Security Scanning](security-scanning.md) — the deterministic Checkov/Trivy layer this critique sits on top of (and the only one that can *gate* a run).
- [AI Plan Summary](ai-plan-summary.md) — the shared switch, provider matrix, daily budget, and prompt-customisation surface.
- [Cost Estimation](cost-estimation.md) — the other AI layer riding the same switch.
- [Policy-as-Code (OPA)](policies.md) — Rego policy sets, the enforcing governance layer.
- Original feature request: <https://github.com/mattrobinsonsre/terrapod/issues/963>
