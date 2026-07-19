# MCP server (`terrapod-mcp`) — drive Terrapod from an AI agent

Terrapod ships an official **MCP (Model Context Protocol) server** so an
MCP-capable agent — Claude Code / Desktop, Cursor, and others — can drive a
Terrapod instance through curated, RBAC-checked tools. The philosophy: Terrapod
is the **safe, governed hands your agent drives**, not itself an agent. You keep
the platform's guardrails (per-user RBAC, the gated run lifecycle, OPA policy);
the agent gets typed tools to observe and act.

`terrapod-mcp` is a **local stdio binary** the agent spawns on your workstation.
It is an ordinary API client — outbound HTTPS only, authenticated with the
**`tofu login` token you already hold**, holding no privileged access. There is
no in-cluster or shared/remote server to deploy.

## Quick start

### 1. Log in (once per instance)

```sh
tofu login terrapod.example.com      # or: terraform login terrapod.example.com
```

This writes a host-keyed token to `~/.terraform.d/credentials.tfrc.json` — the
same file `terrapod-publish` and the `cloud` backend use. `terrapod-mcp` reads
the token for its `--host`.

### 2. Add the server to your agent

Register one server **per Terrapod instance** with a friendly name. Example for
Claude Code (`~/.claude.json` / project MCP config) or any client that speaks
the standard MCP server-config shape:

```jsonc
{
  "mcpServers": {
    "terrapod-prod": {
      "command": "terrapod-mcp",
      "args": ["--host", "terrapod.example.com", "--name", "terrapod-prod", "--env-hint", "prod"]
    },
    "terrapod-dev": {
      "command": "terrapod-mcp",
      "args": ["--host", "terrapod.dev.example.com", "--name", "terrapod-dev", "--env-hint", "dev"]
    }
  }
}
```

**One server per instance is the safe default.** MCP clients namespace tools per
server, so the agent gets two clearly-labelled tool sets, each reading only its
own host's token. A server bound to dev holds no prod token or host — the agent
**literally cannot touch prod from the dev server**. The `--env-hint prod` makes
destructive-action guidance louder on production.

### 3. Ask your agent

> "List my workspaces on terrapod-prod and show the latest run for `app-prod`."
> "Queue a plan on `app-prod`, then summarise what it will change."

## Auth resolution

In order: `--token` → `$TERRAPOD_TOKEN` (headless/CI) →
`~/.terraform.d/credentials.tfrc.json` for `--host`. Tokens are long-lived,
DB-backed API tokens with a fixed max TTL — **there is no silent refresh**. When
one expires, a tool returns an actionable error the agent relays: *"run
`tofu login <host>` and retry."* An RBAC denial is reported distinctly (re-login
won't help — an admin must grant the capability).

## Flags

| Flag | Purpose |
|---|---|
| `--host` | Terrapod instance hostname (required); also the credentials-file key. |
| `--name` | Friendly server name (e.g. `terrapod-prod`). |
| `--token` | Explicit token (else env / credentials file). |
| `--env-hint` | `prod` / `dev` — louder destructive-op guidance. |
| `--skip-tls-verify` | Skip TLS verification (local/dev only). |
| `--version` | Print version and exit. |

## Tools

Every tool is namespaced `terrapod_*` and carries a safety annotation
(read-only vs destructive) so your MCP host can confirm before mutations.

### Observe (read-only) — ground and diagnose

| Tool | What it does |
|---|---|
| `terrapod_workspace_list` | List workspaces with status, execution mode, lock, drift, labels. |
| `terrapod_workspace_get` | One workspace by id or name — full config + status. |
| `terrapod_run_list` | Recent runs for a workspace (status, plan-only/destroy, has-changes). |
| `terrapod_run_get` | One run's full status incl. Terrapod-native detail (has-changes, drift, resource profile, permitted actions). |
| `terrapod_run_plan_json` | The structured JSON plan output (`tofu show -json`) — reason precisely about resource changes. |

### Act (gated) — the normal run lifecycle

| Tool | Safety | What it does |
|---|---|---|
| `terrapod_run_create` | destructive | Queue a run — **defaults to plan-only** (safe). Apply-capable runs stay gated (VCS/policy/RBAC). |
| `terrapod_run_apply` | destructive | Confirm a planned run so it applies — changes real infrastructure. Only after explicit user approval. |
| `terrapod_run_discard` | — | Discard a planned run without applying. |
| `terrapod_run_cancel` | — | Cancel a non-terminal run. |

### Manage (gated) — shape the estate

| Tool | Safety | What it does |
|---|---|---|
| `terrapod_workspace_create` | — | Create a workspace (name required; execution mode, VCS wiring, agent pool, labels, …). |
| `terrapod_workspace_update` | — | Update a workspace's settings (only the fields you pass change; applies on its next run). |
| `terrapod_workspace_delete` | destructive | Delete a workspace + its Terrapod records. Does **not** destroy the tracked infra — queue a destroy run first. Catalog-managed workspaces are refused. |
| `terrapod_variable_list` | read-only | List a workspace's variables (sensitive values masked). |
| `terrapod_variable_set` | — | Upsert a variable by key (terraform or env; `hcl` for non-string values). |
| `terrapod_variable_delete` | destructive | Delete a variable by key. |

### Ground (read-only) — write correct config against *your* estate

| Tool | What it does |
|---|---|
| `terrapod_registry_module_list` | List the private registry modules published here (name, provider, VCS, status). |
| `terrapod_registry_module_get` | One module by name + provider — source, status, owner, labels. |
| `terrapod_registry_module_interface` | A module version's **inputs + outputs** — the exact surface to author a correct `module` block against it, instead of guessing variable names. |
| `terrapod_registry_provider_list` | List the private registry providers published here. |
| `terrapod_registry_provider_get` | One provider by name — namespace, owner, labels. |

Nothing bypasses the platform: applies obey the workspace's VCS/auto-apply/lock
rules and OPA policy, config changes apply on the next run, and every action is
bounded by your Terrapod RBAC — a read-only token cannot mutate.

## Safety model, in short

- **Per-user RBAC** — the token *is* your identity; tools succeed only where your
  role permits.
- **Gated runs** — creating a run goes through the normal lifecycle; plan-only is
  the default and apply is a separate, confirmed step.
- **Destructive tools are annotated** so the agent/host prompts before mutating.
- **Per-instance isolation** — bind one server per Terrapod; a server can only
  reach the instance it was configured for.

## Contract & versioning

The tool catalogue (names + input schemas) is a committed contract, gated in CI:
adding tools is additive, but removing/renaming/retyping one is a breaking change
for agents that depend on it (per Terrapod's [no-breaking-changes
policy](versioning-and-support.md)). On an incompatible API version the server
warns you through the agent.

## Roadmap (additive)

Observe + gated Act + workspace/variable **Manage** + registry **Ground** have
landed. Continuing additively (no breaking changes): the rest of management
**CRUD** (VCS connections, roles, agent pools, run tasks, notifications,
execution hooks, …), and the tofu-native **`discover`** tool (folding in
[`terrapod-query`](terrapod-query.md)) for onboarding existing resources.

## See also

- [Terraform/OpenTofu provider](terraform-provider.md) — manage Terrapod as code (the declarative counterpart).
- [terrapod-query](terrapod-query.md) — the standalone discovery engine the `discover` tool builds on.
- [Authentication](authentication.md) — API tokens and `tofu login`.
