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
| `terrapod_run_plan_json` | What a plan will do, from its structured JSON plan (`tofu show -json`). The default `view: changes` is compact, usually a few KB: tofu's add/change/destroy counts plus each resource the plan acts on, with only the attributes that change, sensitive values redacted. Narrow it with `address` (prefix or glob) and `actions`; page with `start`/`limit`. `view: full` returns the raw document, paged by `offset` once it is larger than `max_bytes` — often megabytes, and unredacted. |
| `terrapod_run_logs` | The plan or apply LOG — the terraform/tofu output, i.e. *why* a run failed rather than merely that it did. Returns the end of the log by default (a failure is reported last, and an apply log can be megabytes), ANSI stripped; `offset` pages further back. |
| `terrapod_run_cost` | A run's monthly cost estimate — the plan's cost *delta* (projected total, this-run delta, previous, per-resource, unpriced). Data only, no AI. |
| `terrapod_workspace_cost` | A workspace's *current* monthly cost from its latest state — total, per-resource, unpriced, and which state version was priced. Data only, no AI. |
| `terrapod_deleted_workspace_list` | Deleted workspaces whose state is still recoverable — name, when and by whom, how many state versions survive, when the window closes, and whether it has already been restored. Platform admin only. |
| `terrapod_run_security_scan` | A run's IaC security-scan result (Checkov/Trivy): engine, enforcement level, threshold, outcome, the normalised findings (rule, severity, resource, file:line), and any override. Null when the workspace does not scan. |
| `terrapod_workspace_architecture_critique` | The AI architecture critique of a workspace's *deployed* system, from its latest state (the optional `ai_architecture` feature) — unlike a plan summary, which reviews a change. Every finding is grounded in the scanner, the cost engine or the resource graph. |
| `terrapod_role_reach` | Which workspaces a custom RBAC role actually grants on, and why — each match with the label or name rule responsible. Use it before changing a role. |
| `terrapod_resource_access` | The inverse: which roles can reach one resource, with the rule responsible and the capabilities each resolves to. |
| `terrapod_vault_status` | Each configured OpenBao (or HashiCorp Vault) instance's sampled status: reachable, sealed, standby and version from `sys/health`; whether Terrapod can log in, and its token's TTL; which trust store TLS used; and the last resolution failure from any run. Unknown is `null`, never `false`. Never a secret value. Admin or audit. |
| `terrapod_vault_reference_check` | Check an OpenBao/Vault reference, or a stored variable's, without resolving it. It asks the server whether Terrapod may read the path, reading nothing there; for kv-v2 only, it lists key **names** and says which fields the reference needs but are missing. A dynamic engine is never read, because a read mints a credential. Needs `var:write` on the workspace, or admin for a variable set. |
| `terrapod_ha_status` | This deployment's HA posture: the leader/follower pair (in sync, seconds since the last sync, classes still backfilling — read these before a failover) and the in-cluster component health. |

### Act (gated) — the normal run lifecycle

| Tool | Safety | What it does |
|---|---|---|
| `terrapod_run_create` | destructive | Queue a run — **defaults to plan-only** (safe). Apply-capable runs stay gated (VCS/policy/RBAC). |
| `terrapod_run_apply` | destructive | Confirm a planned run so it applies — changes real infrastructure. Only after explicit user approval. |
| `terrapod_run_discard` | — | Discard a planned run without applying. |
| `terrapod_run_cancel` | — | Cancel a non-terminal run. |
| `terrapod_run_retry` | destructive | Queue a **new** run from a finished one, with the same configuration version and options, and return it. A plan-only run retries as plan-only; an apply-capable run follows the workspace's auto-apply setting, so treat it like an apply. Needs the same permission as queuing that kind of run. Refused on a run that hasn't finished. |
| `terrapod_module_autodiscovery_rule_scan` | — | Register the modules a module autodiscovery rule finds — every candidate, or just the `subdirectories` you pass (preview first). Skips candidates already registered or whose name is taken, and reports them. Creates registry modules; touches no infrastructure. Platform admin only. |
| `terrapod_run_security_scan_override` | gated | Override a run's blocking IaC security scan so it can proceed despite failed or errored findings; a run held in planning by an enforced scan is re-driven at once. Workspace admin only. This bypasses a security gate — prefer fixing the finding or adding a skip rule. |

### Manage (gated) — shape the estate

| Tool | Safety | What it does |
|---|---|---|
| `terrapod_workspace_create` | — | Create a workspace (name required; execution mode, VCS wiring, agent pool, labels, …). |
| `terrapod_workspace_update` | — | Update a workspace's settings (only the fields you pass change; applies on its next run). |
| `terrapod_workspace_delete` | destructive | Delete a workspace + its Terrapod records. Does **not** destroy the tracked infra — queue a destroy run first. Catalog-managed workspaces are refused. Its **state survives** and stays recoverable for the deployment's retention window (default 30 days) via `terrapod_deleted_workspace_restore`, but recovery yields a NEW workspace with a NEW id and is admin-only — so this is reversible-with-effort, not undoable. |
| `terrapod_deleted_workspace_restore` | destructive | Recover a deleted workspace's state into a **new** workspace. Platform admin only. A salvage operation, not an undo: new id, comes back inert (auto-apply and drift off, VCS not re-attached), variables and run history do not return. Refuses a second restore of the same deletion. |
| `terrapod_variable_list` | read-only | List a workspace's variables (sensitive values masked). |
| `terrapod_workspace_varsets` | read-only | The variable sets that apply to a workspace, and how each one came to apply: explicit assignment, `global`, or an assignment rule. |
| `terrapod_variable_set` | — | Upsert a variable by key (terraform or env; `hcl` for non-string values). |
| `terrapod_variable_delete` | destructive | Delete a variable by key. |

### Ground (read-only) — write correct config against *your* estate

| Tool | What it does |
|---|---|
| `terrapod_registry_module_list` | List the private registry modules published here (name, provider, VCS, status). |
| `terrapod_registry_module_get` | One module by name + provider — source, status, owner, labels. |
| `terrapod_registry_module_interface` | A module version's **inputs + outputs** — the exact surface to author a correct `module` block against it, instead of guessing variable names. |
| `terrapod_module_autodiscovery_rule_list` | The module autodiscovery rules — each names a repository, which directories count as modules (a glob pattern and ignore paths) and how they are named. Platform admin only. |
| `terrapod_module_autodiscovery_rule_preview` | What a rule finds in its repository now: each module directory (root and submodules) with the name and provider it would get, the module already registered from it, and whether its name is taken. Registers nothing. Platform admin only. |
| `terrapod_catalog_item_interface` | A service-catalog item's module interface — the **inputs + outputs** of the module version the item resolves to (its pin, or the latest uploaded version). Needs catalog read on the item. |
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
