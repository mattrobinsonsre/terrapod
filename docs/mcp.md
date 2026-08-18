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

## Install

`terrapod-mcp` ships as a signed release archive on every Terrapod release —
Linux/Windows (amd64 + arm64) and a universal macOS binary — attached to the
[GitHub Release](https://github.com/mattrobinsonsre/terrapod/releases) alongside
the provider and the other CLIs. Download the archive for your platform, verify
it against the `terrapod-mcp_<version>_SHA256SUMS` (signed with the project GPG
key), and drop the `terrapod-mcp` binary somewhere on your `PATH`.

Or build from source with Go:

```sh
go install github.com/mattrobinsonsre/terrapod/mcp/cmd/terrapod-mcp@latest
```

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
| `terrapod_run_logs` | The plan or apply LOG — the terraform/tofu output, i.e. *why* a run failed rather than merely that it did. Returns the end of the log by default (a failure is reported last, and an apply log can be megabytes), ANSI stripped; `offset` pages further back. |
| `terrapod_run_cost` | A run's monthly cost estimate — the plan's cost *delta* (projected total, this-run delta, previous, per-resource, unpriced). Data only, no AI. |
| `terrapod_workspace_cost` | A workspace's *current* monthly cost from its latest state — total, per-resource, unpriced, and which state version was priced. Data only, no AI. |
| `terrapod_deleted_workspace_list` | Deleted workspaces whose state is still recoverable — name, when and by whom, how many state versions survive, when the window closes, and whether it has already been restored. Platform admin only. |

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
| `terrapod_workspace_delete` | destructive | Delete a workspace + its Terrapod records. Does **not** destroy the tracked infra — queue a destroy run first. Catalog-managed workspaces are refused. Its **state survives** and stays recoverable for the deployment's retention window (default 30 days) via `terrapod_deleted_workspace_restore`, but recovery yields a NEW workspace with a NEW id and is admin-only — so this is reversible-with-effort, not undoable. |
| `terrapod_deleted_workspace_restore` | destructive | Recover a deleted workspace's state into a **new** workspace. Platform admin only. A salvage operation, not an undo: new id, comes back inert (auto-apply and drift off, VCS not re-attached), variables and run history do not return. Refuses a second restore of the same deletion. |
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

### Discover (gated) — onboard existing resources

Tofu-native resource discovery: bring existing cloud resources under management.
These tools only **discover and generate** — they produce reviewable `import {}`
blocks + config for the human to adopt; **they never apply an import themselves**.

| Tool | Safety | What it does |
|---|---|---|
| `terrapod_onboard_availability` | read-only | Is the AI-assisted onboarding path available (its own switch + model)? |
| `terrapod_onboard_start` | — | Start a discovery session for a workspace + provider; kicks off credential-less schema discovery. |
| `terrapod_onboard_list` | read-only | A workspace's discovery sessions. |
| `terrapod_onboard_get` | read-only | One session — status, discovery surface (importable data sources), and the generated config + `import {}` blocks. |
| `terrapod_onboard_discover` | — | Run discovery over chosen data-source types → generated config + import blocks (imports nothing). |

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

Observe + gated Act + workspace/variable **Manage** + registry **Ground** +
resource **Discover** have landed. Continuing additively (no breaking changes):
the rest of management **CRUD** (VCS connections, roles, agent pools, run tasks,
notifications, execution hooks, …), and — once the gated import-only apply lands
— an `onboard_apply` tool to adopt the generated import blocks.

## See also

- [Terraform/OpenTofu provider](terraform-provider.md) — manage Terrapod as code (the declarative counterpart).
- [terrapod-query](terrapod-query.md) — the standalone discovery engine the `discover` tool builds on.
- [Authentication](authentication.md) — API tokens and `tofu login`.
