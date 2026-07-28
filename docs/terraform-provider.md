# Terraform / OpenTofu provider (`terraform-provider-terrapod`)

Terrapod ships a **first-class Terraform / OpenTofu provider** —
`terraform-provider-terrapod` — for managing **Terrapod itself as code**. It is
one of the project's four consumer-side Go tools (alongside
[`go-terrapod`](../go-terrapod), [`terrapod-migrate`](migration.md), and
[`terrapod-publish`](registry-publishing.md)), lives in
[`provider/`](../provider), and is built on the Terraform Plugin Framework.

> This is the provider you use to **declare Terrapod's own objects** (workspaces,
> variables, roles, VCS connections, …) in HCL and `terraform apply` them. It is
> **not** the same thing as the [private provider *registry*](registry.md) (which
> distributes *other* providers) or the provider *cache* (a pull-through mirror)
> — "provider" is an overloaded word; this page is specifically about the
> Terrapod Terraform provider.

## What it manages

The provider covers the Terrapod-native management surface — everything you would
otherwise click through in the web UI or call over the API, expressed as HCL:

**Resources** (`terrapod_*`):

| Area | Resources |
|---|---|
| Workspaces | `terrapod_workspace`, `terrapod_autodiscovery_rule`, `terrapod_remote_state_consumer`, `terrapod_module_workspace_link` |
| Variables | `terrapod_variable`, `terrapod_variable_set`, `terrapod_variable_set_variable`, `terrapod_variable_set_workspace` |
| RBAC & identity | `terrapod_role`, `terrapod_role_assignment`, `terrapod_user` |
| VCS | `terrapod_vcs_connection` |
| Agent pools | `terrapod_agent_pool`, `terrapod_agent_pool_token` |
| Governance | `terrapod_run_task`, `terrapod_run_trigger`, `terrapod_notification_configuration`, `terrapod_execution_hook`, `terrapod_execution_hook_workspace` |
| Registry | `terrapod_registry_module`, `terrapod_registry_provider`, `terrapod_gpg_key` |
| Service catalog | `terrapod_catalog_item`, `terrapod_catalog_instance`, `terrapod_provider_template` |

**Data sources** (`terrapod_*`): `terrapod_workspace`, `terrapod_workspaces`,
`terrapod_workspace_cost`, `terrapod_agent_pool`, `terrapod_role`,
`terrapod_user`, `terrapod_vcs_connection`, `terrapod_catalog_instances`.

`terrapod_workspace_cost` reports a workspace's current monthly managed-infra
cost (from its latest state, via the native cost engine) — useful
for budget guardrails and reporting. Example:

```hcl
data "terrapod_workspace_cost" "app" {
  workspace_id = terrapod_workspace.app.id
}

# e.g. gate something on the estimate, or surface it as an output
output "app_monthly_cost" {
  value = data.terrapod_workspace_cost.app.total_max
}
```

Under the hood the provider is a thin, typed wrapper over the canonical Go SDK
[`go-terrapod`](../go-terrapod): the SDK owns every API call shape, and the
provider holds only the Terraform schema, state translation, and lifecycle hooks.
That keeps the provider and the API in lock-step (see the API↔consumer contract
in [`AGENTS.md`](../AGENTS.md)).

## Getting started

### 1. Configure the provider

Each Terrapod instance serves *its own* copy of the provider through the standard
provider-registry protocol at `<your-terrapod-host>/default/terrapod`, so the
`source` is your instance hostname — no public registry account required:

```hcl
terraform {
  required_providers {
    terrapod = {
      source  = "terrapod.example.com/default/terrapod"
      version = "~> 0.3"
    }
  }
}

provider "terrapod" {
  hostname = "terrapod.example.com"
  # Auth token comes from `terraform login terrapod.example.com`
  # or the TERRAPOD_TOKEN environment variable.
}
```

Configuration attributes (all also settable via environment variables):

| Attribute | Env var | Purpose |
|---|---|---|
| `hostname` | `TERRAPOD_HOSTNAME` | The Terrapod instance hostname (e.g. `terrapod.example.com`). |
| `token` | `TERRAPOD_TOKEN` | API token. Prefer `terraform login <host>` (writes `~/.terraform.d/credentials.tfrc.json`) or the env var over hard-coding it. |
| `skip_tls_verify` | — | Skip TLS verification (local/dev only). |

### 2. Declare Terrapod objects

```hcl
resource "terrapod_workspace" "app" {
  name              = "app-prod"
  execution_mode    = "agent"
  execution_backend = "tofu"
  auto_apply        = false

  labels = {
    environment = "prod"
    team        = "platform"
  }
}

resource "terrapod_variable" "region" {
  workspace_id = terrapod_workspace.app.id
  key          = "aws_region"
  value        = "eu-west-1"
  category     = "terraform"
}

# Read an existing workspace by name.
data "terrapod_workspace" "shared" {
  name = "shared-network"
}
```

Then `terraform init && terraform plan && terraform apply` as usual — against
either `terraform` or `tofu`.

## Distribution & signing

- **Per-instance registry.** Every Terrapod instance serves the provider at
  `<host>/default/terrapod` (fetched from GitHub Releases on first request and
  cached in object storage), so `terraform init` resolves it directly from the
  instance you're managing.
- **GPG-signed.** The `SHA256SUMS` are signed with the project signing key and
  the public key is advertised in the download response, so `terraform`/`tofu`
  verify the provider on install.
- **Six platforms.** `linux/{amd64,arm64}`, `darwin/{amd64,arm64}`,
  `windows/{amd64,arm64}`, built and signed by GoReleaser on release.

## Removing a list attribute does not clear it

A handful of `terrapod_workspace` list attributes are `Optional + Computed`:

`agent_pool_ids`, `var_files`, `trigger_prefixes`, `drift_ignore_rules`,
`security_scan_skip_rules`.

For these, **omitting the attribute means "leave alone", not "clear"**. Deleting
the line from your configuration plans as *no changes* and the workspace keeps
its existing value. To clear one, set it to the empty list:

```hcl
resource "terrapod_workspace" "example" {
  # ...
  var_files = []   # clears it; deleting this line would not
}
```

This is deliberate. Terrapod lets these be set outside Terraform — through the
UI, or the bulk-update endpoint — and the provider cannot tell "removed from the
configuration" from "never in the configuration": both reach it as an absent
value over a state value read back from the server. Treating absence as "clear"
would wipe values that are legitimately managed elsewhere.

Because a silent no-op is still a poor experience, the provider **warns at plan
time** whenever a workspace holds a non-empty value for one of these attributes
that your configuration does not declare. If you meant to clear it, set `= []`;
if the value is managed outside Terraform, declare the attribute explicitly to
silence the warning.

## Reserved-name note

Terraform schema attribute names can't be `provider`, so the VCS-connection
resource/data source uses `vcs_provider` for the provider-kind attribute
(`github` / `gitlab`).

## See also

- [`provider/examples/`](../provider/examples) — runnable HCL for the provider
  block, resources, and data sources.
- [Onboarding existing resources](terrapod-query.md) — discover unmanaged cloud
  resources and import them (complements managing new ones with this provider).
- [Migration](migration.md) — `terrapod-migrate` moves existing TFE/HCP or
  Atlantis estates onto Terrapod.
- [API reference](api-reference.md) — the underlying `/api/terrapod/v1` surface
  the provider drives via `go-terrapod`.
