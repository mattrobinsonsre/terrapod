# AI Architecture Critique

The **architecture critic** is an optional AI layer that reviews a workspace's
**deployed system as it exists** — inferred from its latest Terraform state —
and critiques it across **reliability, security, cost, operations, and
scalability**. It is **disabled by default** (`api.config.ai_architecture`).

It is distinct from the [AI plan summary](ai-plan-summary.md):

|  | AI plan summary (#401) | AI architecture critique (#1036) |
|---|---|---|
| **Reviews** | a *change* (a plan) | the *system as it exists* (from state) |
| **Trigger** | every plan | a new state version, or on demand |
| **Scope** | per-run | per-workspace (current state) |
| **Config** | `ai_summary` | its own `ai_architecture` block |

The critic reasons over a large context (the whole workspace's state + resource
graph + cost + security findings), so it gets its **own** config block — a
stronger model and an independent token budget — mirroring `ai_onboarding`.

## Grounded, never invented

Every dimension is anchored in deterministic data; the AI does judgment, not
facts:

- **security** ← the deterministic [security scan](security-scanning.md)
  (Checkov/Trivy). The critic prioritises and contextualises the scanner's
  findings; it never invents a rule. Security findings carry the scanner rule
  id (e.g. `CKV_AWS_24`) in `grounded_in`.
- **cost** ← the native [cost engine](cost-estimation.md). Over/under-provisioning
  and cheaper-alternative findings are grounded in the actual monthly figures.
- **reliability / operations / scalability** ← the state + the resource graph
  (SPOFs, single-AZ, standalone-where-an-ASG-belongs, missing replica/backups,
  deprecated instance types). This is AI judgment over the real resources — it
  never fabricates resource data.

Anything the model could **not** judge from the data it was given is surfaced in
a `deferred` list ("needs attribute review") rather than guessed, so operators
see the gaps instead of a confident hallucination.

Each finding carries a `severity`, a `category`, a specific `recommendation`,
and the exact `resource_address` it anchors to.

## What you get

For a workspace's current state:

- an inferred **architecture** — a 2–4 sentence summary, the tiers, the data
  stores, and where the blast radius concentrates, all grounded in resource
  addresses;
- an overall **risk level** (`low`/`medium`/`high`/`critical`), driven by the
  most severe finding;
- ranked **findings** — a few high-signal items, not a firehose. A
  well-architected system yields an empty list;
- a **deferred** list of concerns the data didn't allow judging.

## How it's triggered

- **Automatically** when a new state version's content is uploaded (after an
  apply, or a manual state push). Best-effort and idempotent — one critique per
  state serial; a re-upload of the same serial doesn't re-run it.
- **On demand** via `POST .../architecture-critique/regenerate` (the workspace
  **Architecture** tab's *Regenerate* button), which forces a fresh critique of
  the current state.

Both paths are no-ops when `ai_architecture.enabled` is false or the workspace
has no state.

## Configuration

```yaml
api:
  config:
    ai_architecture:
      enabled: true
      # Reasons over a large context — prefer a strong model.
      model: "bedrock/anthropic.claude-opus-4-8"
      # Optional OpenAI-compatible base URL for a self-hosted gateway.
      api_base: ""
      # Independent daily budget so a system review can't starve per-run summaries.
      daily_token_budget: 0            # 0 = unlimited
      request_timeout_seconds: 180
      # Extra operator context folded into the prompt (e.g. "prod, PCI scope").
      context: ""
      auth:
        aws_session_name: "terrapod-ai-architecture"
```

The API key / credentials are supplied out-of-band exactly like the other AI
workloads (a `secretKeyRef` env var, or IAM-native auth for Bedrock — see
[AI plan summary → Quick start](ai-plan-summary.md#quick-start)). The critic is
double-gated: the deployment-level `enabled` **and** the presence of AI config;
with AI off, no critique is ever produced.

## Permissions

Reading or regenerating a critique requires **`state:read`** on the workspace —
the critic reasons over the (secret-bearing) state, so it is gated exactly like
reading raw state, not merely viewing the workspace.

## What is NEVER sent to the model

The critic sends a **compacted** view of state: resource addresses, types, and a
curated allowlist of non-secret attributes needed to reason about architecture
(instance types, `multi_az`, replica/backup flags, subnet/AZ placement, sizes).
Secret-bearing attributes are **excluded** by construction. Data sources are
dropped. Large states are truncated to a bounded resource count. See the
[plan-summary redaction model](ai-plan-summary.md#what-is-never-sent) — the same
discipline applies.

## Surfaces

- **Web** — the workspace **Architecture** tab renders the inferred architecture,
  the risk level, and findings grouped by category, with a *Regenerate* action.
  It updates live over the workspace SSE channel
  (`architecture_critique_{pending,ready}`).
- **API** — `GET /api/terrapod/v1/workspaces/{id}/architecture-critique` and
  `POST .../regenerate`. See [api-reference.md](api-reference.md#ai-architecture-critique-terrapod-extension).
- **go-terrapod** — `GetArchitectureCritique` / `RegenerateArchitectureCritique`.
- **MCP** — the `terrapod_workspace_architecture_critique` tool lets an AI agent
  read a workspace's critique (Observe; inherits the caller's `state:read` RBAC).
- **Terraform provider** — the `terrapod_architecture_critique` **data source**
  exposes the risk level and findings as read-only data, e.g. to feed a policy
  check or a report.

## Roadmap

An **estate-wide** critique — reasoning over the cross-workspace topology graph
(shared dependencies, cross-workspace SPOFs) — is planned as a follow-up. The
per-workspace critique described here is the first step.
