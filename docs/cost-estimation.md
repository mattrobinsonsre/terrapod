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

## AI enhancement (separate, optional)

The figures above are always **authoritative oiq-derived data**. An optional AI *enhancement* — a plain-language narrative plus savings advisories (Savings Plans / reserved-instance / spot) — rides the existing [plan-analysis AI switch](ai-plan-summary.md) and renders alongside the data, always flagged distinctly. AI dollar figures are marked `source: ai-estimate` and are **never** blended into the authoritative total and never a gate. With AI disabled, only the data view is shown.

---

## See also

- [API Reference → Cost Estimation](api-reference.md#cost-estimation) — full endpoint shapes.
- [AI Plan Summary](ai-plan-summary.md) — the switch the optional cost narrative rides.
- [MCP](mcp.md) — the `terrapod_run_cost` / `terrapod_workspace_cost` agent tools.
- [OpenInfraQuote](https://github.com/terrateamio/openinfraquote) — the pricesheet and matcher design Terrapod's engine is compatible with.
- Original feature request: <https://github.com/mattrobinsonsre/terrapod/issues/871>
