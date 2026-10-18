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
| Governance | `terrapod_policy_set`, `terrapod_policy`, `terrapod_run_task`, `terrapod_run_trigger`, `terrapod_notification_configuration`, `terrapod_execution_hook`, `terrapod_execution_hook_workspace` |
| Registry | `terrapod_registry_module`, `terrapod_module_autodiscovery_rule`, `terrapod_registry_provider`, `terrapod_gpg_key` |
| Service catalog | `terrapod_catalog_item`, `terrapod_catalog_instance`, `terrapod_provider_template` |
| [Ansible inventory](ansible-inventory.md) | `terrapod_inventory_host`, `terrapod_inventory_group`, `terrapod_inventory_host_group`, `terrapod_inventory_group_child`, `terrapod_inventory_host_var`, `terrapod_inventory_group_var`, `terrapod_inventory_global_var` (the inventory's roots and defaults are attributes on `terrapod_workspace` itself) |

**Data sources** (`terrapod_*`): `terrapod_workspace`, `terrapod_workspaces`
(each entry reports its `engine`, and an optional `engine` argument returns only
that engine's workspaces — narrow with it before feeding the list to anything
that assumes one engine),
`terrapod_workspace_cost`, `terrapod_architecture_critique`,
`terrapod_agent_pool`, `terrapod_role`, `terrapod_user`,
`terrapod_vcs_connection`, `terrapod_catalog_instances`,
`terrapod_catalog_item_interface` (the inputs and outputs of the module version
a catalog item resolves to, with a computed `interface_error` that is non-null
when the module could not be parsed), `terrapod_module_autodiscovery_rule_repositories`
(the repositories a module autodiscovery rule looks at, each with its status,
origin and candidates; kept out of the rule resource so polls never change its
state), `terrapod_oidc_audience_defaults` (the deployment-wide cloud-identity
audience catalogue a workspace's `oidc_audiences` merges over — what a workspace
would INHERIT if it overrode nothing), `terrapod_inventory_resolved` (what a
workspace's [ansible inventory](ansible-inventory.md) resolves to — every host
with its merged variables, each group's **direct** membership, and the nesting
carried separately, with an optional `limit` that ansible itself expands through
that nesting; a data source rather than an attribute on a resource for the same
reason as `oidc_audiences` below, since a data source has no round-trip
requirement and a derived value belongs there).

**`terrapod_workspace.oidc_audiences` holds what you set, not the effective
map**, and that pairing is deliberate. The API returns the merged result, so the
resource reconciles only the keys your configuration declares and ignores the
rest — exactly as `aws_instance.tags` round-trips what you wrote while the
provider's `default_tags` are merged in beneath it. Without that, a plan would
disagree with its own apply the moment the deployment catalogue contained a key
the workspace did not override. Read the effective map off the
`terrapod_workspace` **data source**, which has no round-trip requirement and so
carries it, and the catalogue off `terrapod_oidc_audience_defaults`.

The one case neither can distinguish is an override whose value happens to equal
the catalogue's: it reads as inherited. Nothing changes if you drop it, because
it falls back to the identical default — the same fidelity loss `tags` and
`default_tags` have lived with for years.

A `terrapod_module_autodiscovery_rule`'s `repo_url` may name one repository,
an org or group, or a repository-name pattern such as
`https://github.com/myorg/terraform-*`; the server classifies it on apply and
reports the result in the computed `target_kind`. A glob outside the last path
segment is refused at plan time. `last_error` and `last_enumerated_at` are
computed too. See [Module autodiscovery](registry.md#module-autodiscovery).

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
| `workspace` | `TERRAPOD_WORKSPACE` | The workspace a resource uses when its own `workspace_id` is not set. **A Terrapod run exports this from its own workspace, in both the plan and the apply phase** — so inside a run `provider "terrapod" {}` needs nothing and no resource needs a workspace id. Supply it for a local apply, where there is no run to export it. |

The default applies to **every** resource and data source that takes a workspace id —
`terrapod_variable`, `terrapod_run_task`, `terrapod_notification_configuration`,
`terrapod_run_trigger`, the three workspace joins, the three inventory resources that
carry a workspace id (`terrapod_inventory_host`, `_group`, `_global_var` — the
other four are parented on a host or a group, not a workspace)
and the workspace-scoped data sources. A value you set always wins, which is how a
configuration declares into a *different* workspace from the one its run belongs to.

Attributes that name a **role** in a relationship between two workspaces stay
required, because neither end is "this resource's workspace":
`terrapod_run_trigger.source_workspace_id` (defaulting it to the run's own would
declare a workspace that triggers itself), and `terrapod_remote_state_consumer`'s
`producer_workspace_id` / `consumer_workspace_id` — that resource *is* the grant, so
a guess would be wrong in a direction that matters: defaulting the producer when you
meant the consumer grants read of your state rather than requesting read of theirs.

With no workspace from any source — no attribute, no provider `workspace`, no env var
— a plan fails naming all three, rather than applying an empty id against the server.

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

# An OpenBao/Vault-sourced credential delivered as a file (see docs/vault.md). The
# secret never reaches Terrapod's database, the Job spec or the environment:
# the run sees GOOGLE_APPLICATION_CREDENTIALS=/var/run/terrapod/files/gcp/adc.json
# and the file holds the secret. `value` is the reference, not the secret, so
# it reads back unchanged and does not drift.
resource "terrapod_variable" "gcp_credentials" {
  workspace_id = terrapod_workspace.app.id
  key          = "GOOGLE_APPLICATION_CREDENTIALS"
  category     = "env"
  value_source = "vault"
  value = jsonencode({
    mount = "secret"
    path  = "apps/gcp"
    field = "sa_json"
    file  = { name = "gcp/adc.json" } # or "~/.config/gcloud/adc.json" for the runner's home
  })
}

# A file built from several fields of ONE OpenBao/Vault read (see docs/vault.md,
# "Templates, formats and encoding"): an AWS shared-credentials file from a
# single aws/creds lease, so the key id and secret always belong together.
# A template names no `field`. `{{ }}` is not Terraform interpolation, so it
# needs no escaping inside jsonencode.
resource "terrapod_variable" "aws_credentials_file" {
  workspace_id = terrapod_workspace.app.id
  key          = "AWS_SHARED_CREDENTIALS_FILE"
  category     = "env"
  value_source = "vault"
  value = jsonencode({
    engine = "dynamic"
    mount  = "aws"
    path   = "creds/deploy"
    file = {
      name     = "~/.aws/credentials"
      template = <<-EOT
        [default]
        aws_access_key_id     = {{ access_key }}
        aws_secret_access_key = {{ secret_key }}
      EOT
    }
  })
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
- [API reference](api-reference.md) — the underlying `/api/v1` surface
  the provider drives via `go-terrapod`.


## Tool ↔ API version compatibility

From Terrapod v1.4.0 the provider compares its own build version against the
API's during `Configure` and raises a **warning diagnostic** on a mismatch. It
appears on every plan and apply against a skewed instance:

```
Warning: Terrapod API version mismatch
```

It is a warning, never an error — the plan and apply proceed. It exists so a
provider several minors adrift of the instance is visible before it produces a
confusing failure, rather than after. Align the two versions to silence it. A
provider built from source reports itself as `dev`, which the SDK treats as
"cannot compare" and skips, so local development is unaffected.

## `auto_apply_mode`

`terrapod_workspace` carries both `auto_apply` (boolean) and `auto_apply_mode`
(`never` / `always` / `create` / `create_update`). The mode is the richer form:
the conditional values apply a successful plan only when its shape is within the
declared standard, and **never** auto-apply a plan containing a destroy or a
replace.

**Set one or the other, not both.** The API rejects a request carrying both, and
the provider mirrors that: when `auto_apply_mode` is configured, a configured
`auto_apply` is dropped from the request rather than sent alongside it. If you
set both in HCL you will get the mode's behaviour and the boolean will appear to
have been ignored — because it was. Configure the mode alone:

```hcl
resource "terrapod_workspace" "app" {
  name            = "app-prod"
  auto_apply_mode = "create_update"   # not auto_apply
}
```

Both attributes are `Optional + Computed`, so a configuration that manages
neither will not drift when the server projects one from the other.

## `plan_apply_enabled` and `configure_enabled`

`terrapod_workspace` and `terrapod_autodiscovery_rule` each declare which
operations the workspace has — or, for a rule, which operations the workspaces it
materialises will have — with two independent booleans:

| Attribute | Default | The workspace has |
|---|---|---|
| `plan_apply_enabled` | `true` | a plan/apply operation: the ordinary OpenTofu, Terraform or Pulumi run |
| `configure_enabled` | `false` | a configure operation: an Ansible run against the workspace's [inventory](ansible-inventory.md) |

Omit both and you get today's behaviour — a plan/apply workspace with no Ansible
— which is why adding these attributes drifts nothing on an existing
configuration.

**Setting both to `false` is refused by the API with a 422**, so it fails at
apply rather than silently creating a workspace that can never produce a run.
These booleans are not the way to park a workspace: `locked` is read-only here
(the provider projects it, and locking is an action on the API or in the UI), so
if you want a workspace to stop running without giving up an operation, lock it
there rather than trying to express it in HCL.

```hcl
resource "terrapod_workspace" "fleet" {
  name              = "fleet-prod"
  configure_enabled = true    # Ansible as well as plan/apply
  config_directory  = "ansible"

  # The collections a configure installs, declared on the platform rather than
  # only in the repository's own requirements.yml. Installed after that file and
  # with --force, so a version here overrides a committed one and every other
  # committed collection stands.
  ansible_collections = [
    { name = "community.general", version = "9.2.0" },
    { name = "acme.internal" },   # omit version for whatever resolves
  ]
}
```

`ansible_collections` is collections only — a **role** is a different mechanism
and is declared in the configuration's own `requirements.yml` under `roles:`,
where a git source clones using the workspace's `git_http_auth` /
`git_ssh_auth` credentials. Collections are usually external so they come
through Terrapod's Galaxy proxy; roles are usually in-house so they come from
git. Omitting the attribute leaves the stored list untouched; set
`ansible_collections = []` to clear it.

The refusal is evaluated against the row the write would produce, not against
what the configuration sends, so turning `plan_apply_enabled` off is accepted on
a workspace that has `configure_enabled` set and refused on one that does not.

**`config_directory` is an ordinary path and does not declare anything.** `""`
is its default and means the configuration version's root, exactly as it does
for `working_directory`. It was briefly nullable, with `null` meaning "no
configure operation"; that meaning now lives in `configure_enabled`, and the API
refuses an explicit `null` rather than coercing it — which is what keeps a plan
agreeing with its result.
