# Cross-Workspace Remote State

Terrapod supports `terraform_remote_state` for **cross-workspace composition** — one workspace's runs reading another workspace's outputs — with a producer-controlled allowlist so the state owner stays in charge of who may read its (secret-bearing) state.

This is the standard Terraform pattern for composing workspaces. It complements (and is independent of) [run triggers](run-triggers.md): a run trigger declares *"re-run me when A applies"* (ordering); a remote-state grant declares *"B may read A's state"* (data + authorization). Neither implies the other — see [Run triggers vs. remote state](#run-triggers-vs-remote-state) below.

## Quick start

In the consumer workspace's Terraform configuration:

```hcl
data "terraform_remote_state" "shared" {
  backend = "remote"
  config = {
    hostname     = "terrapod.example.com"
    organization = "default"
    workspaces   = { name = "shared-network" }
  }
}

resource "example_thing" "x" {
  vpc_id = data.terraform_remote_state.shared.outputs.vpc_id
}
```

For this to succeed on agent-mode runs, the **producer workspace** (`shared-network` in this example) must explicitly authorize the consumer workspace. **Default is not shared** (secure by default).

## Authorizing a consumer

Authorization always lives with the producer (the state owner). Three equivalent ways to set it:

**1. Terrapod provider — standalone resource (cross-config / cross-team).** Suitable when producer and consumer workspaces live in different Terraform configurations or are managed by different teams:

```hcl
resource "terrapod_remote_state_consumer" "shared_to_app" {
  producer_workspace_id = terrapod_workspace.shared_network.id
  consumer_workspace_id = terrapod_workspace.app.id
}
```

This still requires admin/write on the *producer* workspace to apply — a consumer team cannot self-grant.

**2. Terrapod API directly:**

```sh
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  "https://terrapod.example.com/api/terrapod/v1/workspaces/ws-PRODUCER/remote-state-consumers" \
  -d '{"data": {"relationships": {"consumer": {"data": {"id": "ws-CONSUMER", "type": "workspaces"}}}}}'
```

List, replace, and revoke endpoints are documented in [the API reference](api-reference.md#cross-workspace-remote-state-consumers).

**3. Bulk-update or autodiscovery rule templates.** For fleets (e.g. a monorepo where many workspaces consume a `shared-network` workspace), set the consumer list via the [bulk-update endpoint](api-reference.md) or an [autodiscovery rule template](autodiscovery.md) so newly-discovered workspaces auto-consume the shared producer.

## Authorization model

| Principal | Reading another workspace's state via the v2 state endpoints |
|---|---|
| **User / API token** (CLI, UI, automation) | Existing label-RBAC: requires `plan` on the producer. Unchanged. |
| **Runner token** (agent-mode run reading via `terraform_remote_state`) | Allowed iff the consumer workspace (the run's own workspace) appears in the producer's allowlist, **or** the consumer workspace *is* the producer (self-reads are harmless — the runner already owns its own state via the run-artifact path). |

Granting / revoking always requires **admin/write on the producer** — by either the provider, the API, or the bulk-update endpoint. There is no way for a consumer to self-grant.

### Security note: state holds secrets

`terraform_remote_state` exposes the **entire state file**, not just the outputs you reference. State files routinely contain sensitive values (passwords, keys, tokens). Treat granting `terraform_remote_state` access the same way you treat granting read access to the producer's secret store. Grant deliberately, audit the grants, revoke when no longer needed.

This is why the design is producer-controlled and default-off, and why granting requires admin on the producer.

## Local vs. agent execution

- **Local execution mode (CLI):** uses your API token directly. `terraform_remote_state` against another workspace works as long as your token has `plan` on the producer. The consumer-allowlist isn't consulted — the existing per-user RBAC governs.
- **Agent execution mode:** the runner uses a short-lived runner token scoped to its own run (`everyone` role). User RBAC doesn't apply to the runner. Cross-workspace state reads succeed iff the consumer workspace is in the producer's allowlist.

If you're seeing a 403 on a cross-workspace read in agent mode, the producer hasn't granted your consumer — see [the runbook](runbooks.md).

## Run triggers vs. remote state

These are **independent** composition edges that often appear together but neither implies the other:

| | Run trigger | Remote-state grant |
|---|---|---|
| Owned by | Consumer (destination) | Producer (state owner) |
| Says | "Re-run me when A applies" | "B may read my state" |
| Carries | Ordering / causality | Data + authorization |
| Required when | You want B to re-plan after A changes infra | B's config uses `data "terraform_remote_state"` against A |

**You can need a run trigger without a remote-state grant.** Example: workspace `app` reads VPC details via `data "aws_vpc" { tags = {…} }` directly from AWS, *not* via `terraform_remote_state`. It still depends on `shared-network` (re-plan after VPC changes) — set up a run trigger `shared-network → app`. But never grant `app` access to `shared-network`'s state — it doesn't need it, and giving the grant would needlessly expose `shared-network`'s secrets.

**You can need a remote-state grant without a run trigger.** Example: `app` reads `shared-network.outputs.vpc_id` once and re-plans only on its own changes. Grant the read; no trigger needed.

In short: trigger answers *when*; grant answers *what + who-may-read*.

## What happens when…

- **The producer hasn't applied yet.** The consumer reads the producer's *current* state version at plan time. If the producer has never applied successfully, there is no state to read — the consumer's `terraform_remote_state` data source fails. Order the first apply with a run trigger or a one-time manual apply on the producer first.
- **The producer is archived (e.g. via [autodiscovery lifecycle](autodiscovery.md)).** The state still exists until purged — consumers' reads continue to succeed as long as the state version remains in storage. Once the workspace is hard-deleted, consumer reads will fail.
- **You delete the consumer workspace.** The grant row is cleaned up automatically (cascade).
- **Cycles (A consumes B, B consumes A).** Allowed — state reads are point-in-time against the *current* state version; no ordering is implied and no deadlock occurs. Whether this models your dependencies correctly is a different question.

## Limitations

- Cross-instance reads (a workspace on Terrapod instance X reading state from Terrapod instance Y) are not supported. State sharing is within a single Terrapod deployment.
- Outputs-only access (TFE's `tfe_outputs`-style data source that exposes outputs without exposing the full state) is **not** offered. `terraform_remote_state` exposes the full state, including sensitive values. If you need outputs-only sharing as a future hardening, file an issue.
- The `terraform_remote_state` data source must use `backend = "remote"` (or the equivalent `cloud {}` block in the consumer's config). The `http` backend variant against Terrapod's state-download URL is unsupported.
