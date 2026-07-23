# Cost Estimation

Terrapod estimates the **monthly cost** of the infrastructure a workspace manages, both as a per-plan *delta* on every run and as the *current* total on a workspace. The numbers are data — no AI, no guesswork — so you can read the price of a change before you apply it and see what a workspace is costing you today.

Cost estimation is powered by a **native, pure-Python reader engine that is compatible with — and consumes the published pricesheet of — [OpenInfraQuote](https://github.com/terrateamio/openinfraquote)** (oiq, by Terrateam, MPL-2.0). Terrapod ships **no binary and shells out to nothing**: it downloads oiq's `prices.csv` and matches plan/state resources against it in-process. The pricing data and the matcher/pricer design are OpenInfraQuote's, and Terrapod credits them wherever a cost is shown.

---

## Two views

| View | Answers | Priced from | Where |
|---|---|---|---|
| **Run cost** | "What does this plan change my monthly bill by?" | The run's plan JSON (the *delta*: adds are positive, removes negative) | Run page → **Cost** tab |
| **Workspace cost** | "What is this workspace costing me right now?" | The workspace's **latest state version** (every resource a `noop`, `diff` zero) | Workspace page → **Cost** tab |

Both views show the projected monthly total, a per-resource breakdown, and an **unpriced** bucket for resources nothing in the pricesheet matched (a free/unmapped type, or a provider the data doesn't cover). The workspace view also names the state version it priced.

## Enabling it

Cost estimation is **on by default** — nothing to turn on:

```yaml
api:
  config:
    cost_estimation:
      enabled: true          # default; set false to disable (endpoints then 404)
      # prices_url: ...       # override the upstream pricesheet (e.g. an internal mirror)
      # default_region: us-east-1   # fallback only — region is resolved per-resource
```

- **Region is resolved per resource** — from the resource's own attributes (`region`/`location`), then its provider config, and only then the `default_region` fallback.
- The pricesheet is a **pull-through cache**: it is mirrored into object storage on first use (no schedule, no extra Helm wiring), and a stale copy is served if a refresh fails so a transient upstream outage never breaks a run.
- **Air-gapped / restricted-network** deployments pre-seed the cached object or point `cost_estimation.prices_url` at an internal mirror of oiq's `prices.csv`.

## How it works

1. On each agent-mode run, the API tells the runner (per-run, via the listener) whether to estimate cost. The runner fetches the cached pricesheet and runs the engine over the plan JSON, uploading a `cost_estimate.json` artifact (best-effort — a costing failure never fails the run).
2. The **run** Cost tab reads that artifact via `GET /api/terrapod/v1/runs/{run_id}/cost-estimate`.
3. The **workspace** Cost tab prices the latest state version server-side (`GET /api/terrapod/v1/workspaces/{id}/cost-estimate`), gated on `state:read`.

Full request/response shapes are in the [API reference](api-reference.md#cost-estimation).

## Where cost shows up

- **Web UI** — a **Cost** tab on both the run page and the workspace page.
- **AI agents (MCP)** — the [`terrapod-mcp`](mcp.md) server exposes `terrapod_run_cost` and `terrapod_workspace_cost` read-only tools, so an agent can reason about the price of a change alongside its plan JSON.
- **Go SDK** — [`go-terrapod`](terraform-provider.md) exposes `GetRunCostEstimate` and `GetWorkspaceCostEstimate`.

## Group / fleet resources

Some resources bill as **N units** where the priceable unit lives on a nested block or a *referenced* resource — so matching the resource's own attributes isn't enough. The engine resolves these before pricing: it derives the unit type and count, prices the unit through the normal path, multiplies by the count, and folds the total back onto the fleet's line (adding to any direct cost the resource also has — e.g. an AKS cluster's management fee *plus* its node pool VMs).

Covered today: `aws_autoscaling_group` (launch template/config, incl. `mixed_instances_policy`), `aws_eks_node_group`, `aws_emr_cluster`, `azurerm_kubernetes_cluster` (+ `_node_pool`), `azurerm_*_virtual_machine_scale_set`, `google_container_node_pool`, and `google_compute_(region_)instance_group_manager`. Coverage is a declarative table, so new fleet types are a data change. A fleet whose unit type isn't yet in the pricesheet (Redshift, MSK, OpenSearch, Fargate, …) is recognised but stays in the **unpriced** bucket until that unit's pricing lands — never a crash, never a wrong number.

## Usage assumptions (honest ranges for usage-driven costs)

Some resources can't be priced from state alone — the bill depends on **runtime usage the plan doesn't declare**: a NAT gateway's cost is dominated by data processed, a Lambda function by invocations, an S3 bucket by stored volume. For these, the deterministic engine prices a **typical** usage point and surfaces the underlying band alongside the number, so the estimate is honest about what it assumed. This is **raw data, available with the AI layer off** — the AI *narrows* an already-useful band; it is never a prerequisite for the number making sense.

Each such resource carries a `usage_assumptions` array, one entry per metered dimension:

| Field | Meaning |
|---|---|
| `description` | Human-readable label for the assumption |
| `dimension` | What is being metered (e.g. `data processed`) |
| `unit` | Unit of the quantity bounds (e.g. `GB/month`) |
| `low` / `typical` / `high` | The assumed **quantity** band |
| `cost_low` / `cost_typical` / `cost_high` | The resulting monthly **cost** at each quantity — the dollar impact of the assumption. Omitted if the band couldn't be priced |

The `monthly` figure folds in `cost_typical` (so workspace totals stay at a sensible point estimate, not the high bound); the true cost of that line item sits somewhere in `cost_low`–`cost_high` as usage varies. A NAT gateway, for instance, is priced at ~$4.50/mo of data at typical usage, but `cost_high` shows it would cost **~$2,250/mo** if it processed 50 TB — the honest upper bound, visible without turning on the AI.

Both Cost tabs render this beneath the resource (e.g. *"data processed: $4.50/mo typical — $0.45–$2,250 across 10–50000 GB/month"*); the `terrapod_workspace_cost` data source and the `go-terrapod` SDK expose the same fields on each resource. Bounds are set to be honest raw — a `high` covers a genuinely busy resource, not a token value — so an operator reading the deterministic estimate can judge where their real workload sits.

## Correctness

The estimates are **deterministic data**. During the port, the native engine was
validated bit-exact against the reference implementation over a committed
plan/state corpus on the *same* pricesheet — that one-off oracle comparison is
complete, and Terrapod no longer depends on any third-party cost engine or its
hosted feed (Terrapod now generates its own pricesheet — see the self-generated
pricesheet, #893/#1025). Ongoing correctness is asserted by the committed engine
unit tests (`test_cost_engine.py`) and the end-to-end pricegen contract tests
(`test_cost_pricegen_contract.py`), which check real priced outputs against known
expected values. No third-party cost binary is ever shipped in a Terrapod image.

## AI enhancement (separate, optional)

The figures above are always **authoritative oiq-derived data**. An optional AI layer rides the existing [plan-analysis AI switch](ai-plan-summary.md) (`api.config.ai_summary.enabled` + the per-workspace mode) and renders **beside** the data on the run Cost tab — never blended into it. With AI disabled, only the data view shows. Every AI dollar figure is tagged `source: "ai-estimate"`, shown separately, and is **never** summed into the authoritative oiq total and never a gate.

Its layers, in order of importance:

- **Estimated resources (primary).** The model prices what the deterministic engine *couldn't* — the `unpriced` bucket: unmapped resource types, providers the pricesheet doesn't cover (e.g. Azure/GCP), and usage-driven costs. Each estimate is a monthly range with a one-line basis (the model's assumption), flagged as an estimate.
- **Savings advisories (secondary).** Opportunities such as Savings Plans, reserved instances, spot, and right-sizing, each with an optional monthly-saving range.
- **Narrative (tertiary).** A short plain-language summary of the estimate.
- **Chat.** A follow-up Q&A thread grounded in the estimate — ask why a resource is expensive, or what a cheaper instance class would cost. One shared thread per run (anyone with workspace read), gated by the same per-run message cap and daily token budget as the plan-summary chat. The model answers from the estimate only and keeps computed-vs-estimated figures distinct.

**Languages.** Like the plan analysis, the AI cost prose (narrative, each estimate's basis, advisory text, and chat replies) is generated in the deployment's `ai_summary.summary_language` and **translated on view** into the reader's UI locale (best-effort, cached); the authoritative oiq figures are locale-agnostic numbers.

The authoritative figures always live on the data-only cost-estimate endpoint and are never restated by the AI layer.

---

## See also

- [API Reference → Cost Estimation](api-reference.md#cost-estimation) — full endpoint shapes.
- [AI Plan Summary](ai-plan-summary.md) — the switch the optional cost AI rides.
- [MCP](mcp.md) — the `terrapod_run_cost` / `terrapod_workspace_cost` agent tools.
- [OpenInfraQuote](https://github.com/terrateamio/openinfraquote) — the pricesheet and matcher design Terrapod's engine is compatible with.
- Original feature request: <https://github.com/mattrobinsonsre/terrapod/issues/871>
