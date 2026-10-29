# Service Catalog

The Service Catalog turns blessed entries in your [private module registry](registry.md) into **no-code, self-service provisioning** flows. A platform admin designates a registry module as a *catalog item*; an authorised user then fills in a form and gets a fully managed Terrapod workspace — no HCL to write, no VCS connection to wire up, no `cloud {}` block to copy.

The catalog is built on infrastructure Terrapod already has — the module registry, agent-mode workspaces, the module→workspace impact link, and label-based RBAC — so it is a thin, opinionated layer rather than a parallel system.

## When you'd want this

- A platform team maintains a set of golden modules (a VPC module, a managed-database module, an application-stack module) and wants application teams to consume them without learning terraform.
- You want self-service provisioning with guardrails: blessed inputs, a fixed set of provider configurations, a controlled list of agent pools.
- You want every provisioned instance to be tracked, reconfigurable, and destroyable from one place.

## When you wouldn't

- Your users write their own root modules and just need workspaces. Use ordinary [workspaces](getting-started.md) or [autodiscovery](autodiscovery.md).
- You don't run a private module registry. The catalog has nothing to point at.

## What a catalog item is

A **catalog item** is a blessed designation over one registry module — it does not copy or fork the module. It records:

- which module it wraps (`module-id`),
- which **provider templates** render its provider configuration,
- which **agent pools** an instance may bind to (optionally restricted),
- a curated set of **variable options** (defaults, descriptions, sensitivity, enum choices) layered over the module's own variables,
- a **version policy** (float, or a default pin),
- optionally, the **Ansible half** it provisions — whether the instance has a configure operation, which subdirectory of the module holds the playbook, and the default playbook and limit (see [Provisioning an Ansible-first workspace](#provisioning-an-ansible-first-workspace)).

## What provisioning produces

Provisioning a catalog item creates an ordinary **agent-mode, non-VCS workspace** — the same kind of workspace you'd get from the API — whose configuration is a **server-generated wrapper** around the registry module. The user never sees or edits HCL.

The generated configuration is a small, predictable set of files:

```hcl
# main.tf  (generated) — the module call plus one root `variable` block per
# catalog input. The variable declarations are intentionally UNTYPED: the
# module-interface `type` string isn't reliably valid HCL for complex types
# (object/tuple), so the wrapper omits it and lets the supplied value carry its
# own type. Values arrive correctly-typed as workspace terraform variables.
module "this" {
  source  = "terrapod.example.com/default/vpc/aws"
  version = "1.4.0"          # resolved from the item's version policy / instance pin

  cidr_block = var.cidr_block # one passthrough per declared input
  name       = var.name
  # ...
}

variable "cidr_block" {}
variable "name" {}
# a sensitive input adds only the marker:  variable "secret" { sensitive = true }

# providers.tf  (generated) — rendered from the item's provider templates
provider "aws" {
  region = var.aws_region
}

# outputs.tf  (generated) — re-exports the module's outputs
output "vpc_id" { value = module.this.vpc_id }
```

The inputs the user supplied on the provision form become the workspace's **Terraform variables** (`category = terraform`). Sensitive inputs are stored as sensitive variables. The workspace is marked **catalog-managed** by its `catalog_item_id` — that flag is what activates the guardrails described below.

Because the wrapper is generated server-side and the workspace is agent-mode + non-VCS, runs execute on your agent pools exactly like any other agent run: a plan, an approval (or auto-apply), and an apply.

## Enabling the catalog

The catalog is **on by default**. The catalog RBAC axis is opt-in (`catalog_permission` defaults to `none`), so enabling the feature does not grant anyone access — no user can browse or provision until granted catalog permission. To hide the surface entirely, set:

```yaml
# values.yaml
api:
  config:
    catalog:
      enabled: false   # default: true
```

When `catalog.enabled` is `false`, every catalog API endpoint returns `404` and the catalog UI surfaces are hidden. The toggle requires no migration — the tables always exist; the feature is purely gated.

## RBAC: the `catalog_permission` axis

Catalog access is a **dedicated, opt-in permission axis** — like the pool and registry axes, it is carried in the role's `capabilities` set as `catalog:*` tokens (#585). The `catalog-permission` level (`none`/`read`/`use`/`admin`) is authoring shorthand for that slice of the capabilities and a derived read-only summary — it is not a stored scalar. Each axis's capabilities are managed independently, so a role can grant catalog `use` without any workspace access. It is **label-scoped**: a role's allow/deny labels and names decide which catalog items the permission applies to, exactly as for workspaces, pools, and registry resources.

| Level | Grants |
|---|---|
| **none** (default) | No catalog access at all. The catalog is invisible. |
| **read** | Browse the catalog and view instances of items the role matches. |
| **use** | read + provision new instances, reconfigure them, and destroy them. |
| **admin** | Manage catalog items and provider templates (platform admin). |

Two things to note:

- **Default is `none`.** Unlike workspace permissions, there is **no `everyone` floor** for the catalog — a workspace can be world-readable via `access: everyone`, but a catalog item never is. Catalog access is always an explicit grant. This keeps self-service provisioning deliberate: nobody can spin up infrastructure until you've said they can.
- **`admin` is platform admin.** Creating, editing, or deleting catalog items and provider templates requires platform `admin`. The `use` level is what you grant to consuming teams.

Resolution order mirrors the other axes: platform admin → platform audit (read) → label-based RBAC via `catalog_permission` → none (no `everyone` floor).

### Granting catalog access

`catalog_permission` is set on a **custom role**, exactly like `workspace_permission` / `pool_permission` / `registry_permission` — via the admin **Roles** page, the API (`catalog-permission` attribute on `POST/PATCH /api/v1/roles`), the `terrapod_role` provider resource (`catalog_permission`), or `go-terrapod`'s `CatalogPermission` field. Assign the role to a user/group, scope it with the role's allow/deny labels, and that user can browse (`read`) or self-serve (`use`) the matching catalog items. Until a role grants it, only platform `admin` (and an item's owner) can reach the catalog.

## Catalog-managed workspace guardrails

A workspace created by the catalog is **catalog-managed**, and the catalog owns its configuration and access. Two guardrails enforce this so an instance can't drift away from its catalog definition out of band.

### RBAC clamp — instances are read-mostly

On a catalog-managed workspace, **non-platform-admin grants are clamped to `read`**, with one exemption. Whatever a user's owner / label / `everyone` grant would normally give them on this workspace (write, admin) is capped at read: the user who provisioned the instance gets **read**, and manages it through the catalog rather than through the workspace settings API. Platform admins are not clamped.

The exemption is **`inventory:write`**, so that an [Ansible-first instance](#provisioning-an-ansible-first-workspace) can have its inventory curated by the person who provisioned it — a host the module's data source does not find, a group that only means something to an operator. Everything else a write grant would carry, the variables and the configuration included, is still clamped away, and a row the module declares is still reverted by the next apply. Both of those are covered in [The inventory stays writable](#the-inventory-stays-writable--and-a-hand-edit-does-not-survive-the-next-apply).

The effect: you can't bypass the catalog by editing the instance workspace's variables or VCS settings directly, and you can't elevate yourself to admin on it via a label trick. The catalog surface (`use` permission) is the management path; the workspace surface is observe-only for everyone except platform admins.

### Config clamp — the catalog owns the configuration

Because the catalog generates the wrapper, an instance must not accept a configuration from anywhere else:

- A direct **configuration-version upload** to a catalog-managed workspace is rejected with `409`.
- A **run that pins a custom configuration version** on a catalog-managed workspace is rejected with `409`.

Configuration changes flow through **reconfigure** (below), which regenerates the wrapper. Manage instances through the catalog surface, not the workspace run/config API.

## Provider templates

Provider configuration is the one part of a wrapper that varies per environment (region, endpoint, assume-role, profile) and that you don't want every provisioner inventing. **Provider templates** are admin-managed, parameterised provider configs that render into the generated `providers.tf`.

A provider template has:

| Field | Description |
|---|---|
| `name` | Display name, unique. |
| `provider-type` | The provider it configures, e.g. `aws`, `google`, `azurerm`. |
| `body` | An HCL provider body that references `var.*`. |
| `parameters` | Declared parameters that become **Terraform variables** on every instance using this template. |
| `labels` | For label-based RBAC. |

Example:

```hcl
# provider template "aws-standard", provider-type = aws
provider "aws" {
  region = var.aws_region
  assume_role {
    role_arn = var.aws_role_arn
  }
}
```

with `parameters`:

```json
[
  { "name": "aws_region",   "type": "string", "description": "Target AWS region" },
  { "name": "aws_role_arn", "type": "string", "description": "Role to assume" }
]
```

When an item references this template, `aws_region` and `aws_role_arn` are surfaced as fields on the provision form, and the provided values are injected as Terraform variables that the rendered `providers.tf` reads via `var.*`.

> **No server-side interpolation.** The server does not substitute values into the template body — it renders the static `body` verbatim and lets terraform/tofu resolve `var.*` at plan time from the workspace's variables. Parameters become real Terraform variables; the provider body references them. This keeps the rendering simple and auditable, and means a sensitive parameter is protected by the same variable encryption-at-rest as any other sensitive variable.

A catalog item lists the templates it uses in `provider-template-ids`. Each one's `parameters` are merged into the provision form alongside the module's own inputs.

## Curating inputs — variable options

By default the provision form exposes **every** module input. A catalog item can curate them with `variable-options` — a list of per-input overlays, one object per input you want to change:

```json
"variable-options": [
  { "name": "environment", "options": ["dev", "staging", "prod"] },
  { "name": "instance_type", "default": "t3.small" },
  { "name": "account_id", "hidden": true, "default": "123456789012" }
]
```

Each overlay object keys off the module input `name` and supports:

| Key | Effect |
|---|---|
| `options` | Constrain the input to an **allow-list** (rendered as a dropdown). A value outside the list is rejected **server-side** (`422`), not just hidden in the UI — this is a real governance control, not a hint. |
| `default` | Preset the input's default. The user can still edit it (unless also `hidden`). Useful for org-preferred values. |
| `hidden` | **Fix the value and remove the input from the form.** A hidden input is wired from its `default` and the provisioner never sees or sets it; attempting to supply it returns `422`. **A `hidden` overlay must include a `default`** — the API rejects `hidden` without one (a hidden, required-by-the-module input with no value fails opaquely at plan time otherwise). |
| `sensitive` | Mark the input sensitive (masked input field; stored as a sensitive workspace variable, write-only). |

This is how a catalog author pins certain module variables: mark them `hidden` with a fixed `default` and the consumer provisions without ever touching them. Validation runs at item create/update — a malformed overlay, a non-list `options`, or `hidden` without `default` is rejected with `422`.

## Provisioning an Ansible-first workspace

A catalog item can provision a workspace that is **primarily Ansible**, carrying only as much Terraform as it takes to declare the inventory the playbook then runs against. The consuming team fills in the same provision form; what they get is a workspace with a [configure operation](ansible-configure.md) already declared, an inventory the apply wrote, and a playbook that came out of the blessed module.

Four attributes on the catalog item say so — `configure-enabled`, `ansible-directory`, `default-playbook`, `default-limit`. Each is templated onto the provisioned workspace exactly as `execution-mode` and the engine version already are: nothing else in the catalog is aware of Ansible, and an item that sets none of them provisions what it provisioned before.

| Item attribute | Default | What it does |
|---|---|---|
| `configure-enabled` | `false` | Whether the provisioned workspace **has** a configure operation. This is the declaration; the other three mean nothing without it. |
| `ansible-directory` | `""` | The **subdirectory of the module** whose files are copied into the generated configuration version. It becomes the provisioned workspace's `config-directory`. Required when `configure-enabled` is on. |
| `default-playbook` | `""` | Templated onto the workspace's `default-playbook` — the playbook a configure runs when the run names none. |
| `default-limit` | `""` | Templated onto the workspace's `default-limit` — the blast radius. **Empty means every host in the inventory**; see [`default-limit` is the blast radius](ansible-configure.md#default-limit-is-the-blast-radius-and-empty-means-everything). |

Turning `configure-enabled` off again on the item is a real instruction, not a no-op: the next reconfigure clears all four on the instance, so the workspace is left with the operation **gone** rather than with a stale `config-directory` and a playbook it no longer carries.

Set them on the item through the **catalog-item API** — the `attributes` block of `POST`/`PATCH /api/v1/catalog-items`, alongside `module-id` and `variable-options` — or declaratively on the `terrapod_catalog_item` resource, whose attributes are the same names in snake_case. Keeping the catalog [as code](#managing-the-catalog-as-code-terraform-provider) therefore covers the Ansible half like the rest of the item:

```hcl
resource "terrapod_catalog_item" "app_stack" {
  name      = "app-stack"
  module_id = terrapod_registry_module.app_stack.id

  configure_enabled = true
  ansible_directory = "ansible"    # a subdirectory of the MODULE
  default_playbook  = "site.yml"   # relative to ansible_directory
  default_limit     = "canary"     # empty would mean every host
}
```

### What the module has to ship

Two halves, in one module version.

- **Terraform at the module root that declares the inventory.** The hosts the apply creates become `terrapod_inventory_host` rows in that same apply, so the inventory describes the machines that now exist, and the ordering is implicit rather than declared. See [Declaring hosts alongside the infrastructure](ansible-inventory.md#declaring-hosts-alongside-the-infrastructure).
- **A subdirectory holding the Ansible half** — the playbook, your own `ansible.cfg`, `group_vars`, roles. That is the directory `ansible-directory` names.

```
modules/app-stack/
├── main.tf              # the instances, plus terrapod_inventory_host/_group/_host_var
├── versions.tf          # required_providers — the module's own; see below
├── outputs.tf
└── ansible/             # ← ansible-directory = "ansible"
    ├── ansible.cfg      # yours, read as-is
    ├── site.yml         # ← default-playbook = "site.yml"
    ├── group_vars/
    │   └── all.yml
    └── roles/
```

**The module declares its own `required_providers`.** The generated wrapper emits none — it is the module call, one root `variable` per input, the provider template bodies and the output re-exports, and nothing else — so the `terrapod` provider has to be required by the module itself:

```hcl
# versions.tf, inside the module
terraform {
  required_providers {
    terrapod = {
      source = "terrapod.example.com/default/terrapod"
    }
  }
}
```

The `source` is your own instance's hostname, and **there is nothing to publish**: every Terrapod instance serves its own copy of the provider over the standard provider-registry protocol, fetching it on first request and caching it in object storage. See [Distribution & signing](terraform-provider.md#distribution--signing). Two things follow from that:

- The version list serves the **running platform version**, so a `version` constraint on that block has to admit it. Omitting the constraint is the simplest thing that keeps working across a Terrapod upgrade.
- In a **sealed (`registry.cache_only`) deployment** nothing is fetched on demand, so the platform provider has to be warmed into the cache before a run needs it, like any other artifact. See [Sealed (cache-only) mode](runbooks.md#sealed-cache-only-mode--runs-failing-with-cache-miss-404).

Inside the run the provider needs no configuration at all — `provider "terrapod" {}` is the whole block, and no inventory resource needs a `workspace_id`, because the runner exports the run's own workspace and the provider defaults from it. See [`provider "terrapod" {}` needs nothing in it](ansible-inventory.md#provider-terrapod--needs-nothing-in-it).

### The playbook is copied out of the module, not referenced

With `configure-enabled` on, provisioning reads the resolved module version's tarball, takes every file under `ansible-directory`, and writes them into the **generated configuration version** alongside `main.tf`. Each file keeps the path it has in the module, so `ansible/site.yml` lands at `ansible/site.yml` — and the workspace's `config-directory` is therefore just `ansible-directory`, with nothing to translate.

**It is copied because a composite run is two runner Jobs.** The apply runs in one and the configure in another; the configure Job re-fetches the configuration version and never runs `terraform init`, so `.terraform/modules/` does not exist in it. A `config-directory` pointing into the init'd module directory would pass every validator — the path checks refuse traversal, leading and trailing slashes and stray whitespace, and such a path has none of those — and then fail at run time against a directory nothing created. Copying is what puts the Ansible half in the Job that runs it.

Two consequences worth knowing:

- **The Ansible half is versioned with the module**, and it moves when the configuration version is regenerated — which is **provision and reconfigure, and only those**. Bumping an instance's version pin brings the new playbook with it, because a reconfigure re-copies. A publish-triggered run on a [floating](#version-model) instance does **not**: it re-runs the configuration version the instance already has, so a newly published playbook reaches it at its next reconfigure. There is still no second artifact to keep in step — the playbook and the Terraform that declares its inventory are one module version.
- **Only regular files are copied.** A symlink, a hard link or a device entry under `ansible-directory` is dropped rather than followed: this repacks into a tarball a runner extracts, and a link is how a tar carries a path out of its own tree. If a playbook tree symlinks `group_vars/all` at a shared file elsewhere in the repository, the copy will not carry it — commit the real file under `ansible-directory`.

### The inventory stays writable — and a hand edit does not survive the next apply

The [RBAC clamp](#rbac-clamp--instances-are-read-mostly) caps every non-platform-admin grant on a catalog instance at read, with **one exemption: `inventory:write`**. So the person who provisioned the instance can declare and edit inventory rows — a host the module's data source does not find, a group that only means something to an operator — through the API, the UI's per-row inventory view, the MCP tools or the provider, while their access to the instance's variables and configuration stays read-only.

**The cost, stated here rather than left to be discovered: the module declares the rows, so a hand-edited row is reverted by the next apply.** That is OpenTofu's (or Terraform's) own semantics rather than a catalog rule — the configuration is the desired state — and it is exactly what an inventory-declaring module does outside the catalog too. See [Nothing here is read-only](ansible-inventory.md#nothing-here-is-read-only). A row the module does **not** declare is as durable as any other; it is only the declared ones that come back.

Variables and the configuration stay clamped to read deliberately. The catalog generates the wrapper and replaces the variable set on every reconfigure, so a write over either would be an affordance for an edit the next reconfigure silently discards. The inventory is different in kind: it is the part of such a workspace that a person legitimately curates.

### Starting the configure

Provisioning **declares** the operation. It sets no trigger: `configure-after-apply` and `auto-configure-enabled` stay off on a provisioned instance, exactly as they do on any newly created workspace. So the first run is an ordinary apply, which creates the machines and writes the inventory, and nothing configures yet.

Starting a configure needs `run:apply` on the workspace, and the clamp removes that for everyone except a platform admin. On a catalog instance, then, a configure is started by a platform admin — either run by run, or once and for all by turning `configure-after-apply` on for that instance, after which every apply configures. The catalog templates only the four attributes above, so a trigger set that way is **not** reverted by a later reconfigure.

[Running a Configure](ansible-configure.md) covers everything from there: the triggers, the debounce, the narrowing to the hosts an inventory change actually affected, the per-host report and the log.

### Refusals

All `422`, and each aimed at the thing that is wrong rather than at the symptom it would otherwise produce:

| When | What is refused |
|---|---|
| Saving the item | `configure-enabled` on with no `ansible-directory`. The switch says whether and the path says where; on with nowhere to look is incoherent rather than a no-op — it would provision a workspace whose Ansible root is the configuration version's root, where only the generated wrapper lives. |
| Saving the item | An `ansible-directory` or `default-playbook` carrying a leading or trailing slash, a `..` segment, or leading/trailing whitespace. Refused rather than normalised, because the value round-trips through a Terraform provider and storing a different one than you sent is what fails an apply with *"Provider produced inconsistent result after apply"*. |
| Provisioning / reconfiguring | An `ansible-directory` of `"."` — the module's **root**. Copying the module root over the generated wrapper is the one case in which a copied file could shadow `main.tf`, so the form is refused outright. (`""` and a trailing slash are caught earlier, by the two rows above; `"."` passes both of those checks, so it is refused here.) |
| Provisioning / reconfiguring | An `ansible-directory` the resolved module version does not contain. The message names the directory **and what that module version's top level does hold**, so a typo or a renamed subtree is obvious from the error rather than from a run that fails later. |
| Provisioning / reconfiguring | `configure-enabled` on an item whose module has no uploaded version to copy from. |

### Before a team can use this

Neither is new to the Ansible half, but both are explicit grants and both are easy to miss:

- **Catalog access.** `catalog_permission` defaults to `none`, and the catalog has **no `everyone` floor**, so enabling the catalog grants nobody anything until a role carries catalog `use` and is assigned. See [Granting catalog access](#granting-catalog-access).
- **A pool to run on.** Every instance is agent-mode, so the provisioner needs pool `write` on a pool the item allows. See [Agent pools](#agent-pools).

## Version model

A catalog item wraps a registry module, and registry modules are versioned. The catalog supports two version strategies:

- **Float (unpinned).** The instance tracks the latest published module version. This is wired through the standard **module→workspace impact link** plus the registry's **publish triggers**: when a new module version is published, linked instances get a fresh run automatically — the same mechanism documented in [Registry](registry.md) (module impact analysis). With `auto-apply` on, the instance updates itself to the new version with no human in the loop.
- **Explicit pin.** An instance can carry a `catalog_version_pin` that holds it at a specific module version regardless of new publishes. The catalog item defines a `default_version_pin` that new instances inherit; leave it unset for float.

> **Float + auto-apply is a deliberate "brave" choice for production.** It means publishing a new module version can roll out to every floating instance automatically. The provision UI warns when you select it. For production fleets, prefer an explicit pin (or float with auto-apply *off*, so a human confirms each version bump). For ephemeral or dev environments, float-and-auto-apply is a convenient way to keep everything on the latest module.

## Lifecycle

```
provision ──▶ reconfigure ──▶ ... ──▶ destroy ──▶ archived
   │              │                       │
 creates       updates                 queues an is_destroy run
 instance      inputs / re-pins        (source = catalog-lifecycle)
 + first run   + new run               on successful apply: workspace archived
```

- **Provision** creates the catalog-managed workspace, materialises the supplied inputs as variables, generates the wrapper, and queues the first run. Source: `catalog`.
- **Reconfigure** updates an instance's inputs and/or its version pin, regenerates the wrapper, and queues a new run. Source: `catalog`. This is the only supported way to change a catalog instance's configuration (the config clamp blocks the alternatives).
- **Destroy** queues an `is_destroy` run with source **`catalog-lifecycle`**. On a **successful apply** of that destroy run, the workspace is **archived** — a soft delete: the workspace and its **state are retained** (for audit and possible recovery), but the instance is removed from the active catalog view. Nothing is hard-deleted automatically.

### Destroy, not delete — a catalog instance is never silently orphaned

A catalog instance is something you **provisioned**, so its teardown **reclaims the infrastructure** — unlike a plain workspace, where deleting the workspace record deliberately leaves the real infrastructure running for an operator to manage elsewhere.

- **Destroy (recommended).** `POST /api/v1/catalog-instances/{id}/destroy` runs `terraform destroy` and archives the workspace **on a successful apply**. If the destroy fails, the instance stays — the record is never removed while infrastructure might still exist. This is the catalog teardown.
  - **Auto-retry.** `terraform destroy` is commonly transiently flaky (dependency-release ordering, eventual consistency, draining LBs / releasing ENIs / emptying buckets), so a failed catalog destroy is automatically retried a bounded number of times (`runners.lifecycleDestroyRetries`, default **2** → 3 attempts, with `runners.lifecycleDestroyRetryBackoffSeconds` between tries). Re-running is safe — destroy is incremental — and the workspace is still archived only on a *successful* destroy, so retries never lose data. After the cap the instance stays `errored` for an operator. Set the retry count to `0` to disable.
- **The plain workspace delete is blocked.** `DELETE /api/v1/workspaces/{id}` returns **409** for a catalog-managed workspace — deleting it there would silently orphan the provisioned infrastructure. There is no way to orphan a catalog instance by accident.
- **Orphan (explicit, discouraged escape hatch).** `DELETE /api/v1/catalog-instances/{id}?orphan=true` deletes the workspace record and **abandons** the infrastructure (it keeps running, untracked). It requires catalog **`admin`** on the item and the explicit `orphan=true` flag, and is audit-logged. Use it only when the infrastructure is already gone or is owned elsewhere. In the UI it's an admin-only **Orphan…** action on the instance row that opens a confirmation requiring you to type the instance name — deliberately more friction than Destroy. The Terraform provider's `terrapod_catalog_instance` always **destroys** (reclaims) on `terraform destroy`; there is no orphan-on-destroy mode.

**Deleting a catalog item is blocked (`409`) while it still has instances.** Destroy or migrate the instances first; only then can the item be removed. This prevents orphaning live infrastructure whose definition you've thrown away.

## Agent pools

Every catalog instance is an agent-mode workspace, so it **binds to an agent pool**. Two rules govern which pool:

1. **The provisioner must have pool `write`.** Assigning a pool to a workspace already requires pool `write` (see [RBAC](rbac.md#pool-permission-levels)); the catalog enforces the same on provision. The provision form only offers pools the user may actually use.
2. **The pool must be allowed by the item, when restricted.** A catalog item may set `allowed_agent_pool_ids` to a fixed list — instances of that item can only bind to a pool in the list. Set it to `null` to allow any pool the user has `write` on.

The combination lets a platform team say "VPC instances run only on the `network` pool, and only network-team members can provision them" without writing any custom policy.

## UI tour

| Page | Audience | What it does |
|---|---|---|
| `/catalog` | catalog `read`+ | Browse available items (filtered to what your role matches). |
| `/catalog/{id}` | catalog `read`/`use` | Item detail: the provision wizard (form fields resolved from inputs + provider-template parameters) and the list of existing instances. |
| `/admin/catalog` | platform `admin` | Manage catalog items: bless a module, set provider templates, agent-pool allowlist, variable options, version policy, enable/disable. |
| `/admin/provider-templates` | platform `admin` | CRUD provider templates (name, provider-type, HCL body, parameters). |

The provision wizard renders one field per resolved input — the module's variables (as curated by the item's `variable-options`) plus every parameter from the item's provider templates — and shows the version policy with the float/auto-apply warning where applicable.

## Managing the catalog as code (Terraform provider)

The catalog is fully manageable through [`terraform-provider-terrapod`](https://github.com/mattrobinsonsre/terrapod), so a platform team can keep the catalog itself under GitOps and even drive provisioning declaratively:

- `terrapod_provider_template` — a parameterised provider config.
- `terrapod_catalog_item` — a blessed module designation (module reference, provider templates, pool allowlist, variable options, version policy).
- `terrapod_catalog_instance` — a provisioned instance (inputs, agent pool, version pin, auto-apply). Managing instances this way gives you declarative create/reconfigure/destroy with the same lifecycle as the UI.
- `terrapod_catalog_instances` (data source) — enumerate instances of an item.

This means the platform team manages the *definitions* in one repo, and either lets users self-serve through the UI **or** provisions instances declaratively for consuming teams — both go through the same catalog API and the same RBAC.

## API

All endpoints are under `/api/v1`, JSON:API, and gated on `catalog.enabled` (they return `404` when the catalog is off). See [Service Catalog](api-reference.md#service-catalog) in the API reference for the full request/response shapes. In brief:

```
# Provider templates (admin write; admin/audit read)
GET    /api/v1/provider-templates
POST   /api/v1/provider-templates
GET    /api/v1/provider-templates/{id}
PATCH  /api/v1/provider-templates/{id}
DELETE /api/v1/provider-templates/{id}

# Catalog items (admin write; list/show require catalog read, filtered per item)
GET    /api/v1/catalog-items
POST   /api/v1/catalog-items
GET    /api/v1/catalog-items/{id}
PATCH  /api/v1/catalog-items/{id}
DELETE /api/v1/catalog-items/{id}          # 409 while instances exist
GET    /api/v1/catalog-items/{id}/form      # resolved provision form
GET    /api/v1/catalog-items/{id}/interface # the module version's inputs + outputs
GET    /api/v1/catalog-items/{id}/instances
POST   /api/v1/catalog-items/{id}/provision # catalog use + pool write

# Catalog instances (the workspace id is the instance id)
GET    /api/v1/catalog-instances/{wsId}
PATCH  /api/v1/catalog-instances/{wsId}          # reconfigure (catalog use)
POST   /api/v1/catalog-instances/{wsId}/destroy  # destroy (catalog use)
POST   /api/v1/catalog-instances/{wsId}/confirm  # apply a planned run (catalog use)
POST   /api/v1/catalog-instances/{wsId}/discard  # discard a planned run (catalog use)
```

Runs created by the catalog carry distinct sources: **`catalog`** for provision and reconfigure runs, and **`catalog-lifecycle`** for the destroy→archive run.

### Confirming a non-auto-apply run

Provision, reconfigure, and destroy accept `auto-apply`. When it is **false**, the resulting run stops at **`planned`** and waits for a human. Because the catalog-managed-workspace clamp grants the provisioner only **read** on the underlying workspace (plus `inventory:write`, which does not include `run:apply`), the run can't be confirmed through the normal workspace run API — instead use the catalog surface:

- `POST /catalog-instances/{wsId}/confirm` — apply the pending planned run (catalog `use`).
- `POST /catalog-instances/{wsId}/discard` — discard it (catalog `use`).

Both act **only** on a catalog-initiated, apply-capable planned run (`source` ∈ {`catalog`, `catalog-lifecycle`}, not plan-only) — a speculative module-impact plan that happens to be the latest run is never promotable this way. The web UI surfaces these as a pending-run banner on the instance page.

## Related

- [Registry](registry.md) — the private module registry the catalog blesses, plus module impact analysis (the float/publish-trigger mechanism).
- [RBAC](rbac.md) — the permission model the `catalog_permission` axis extends.
- [Agent Pools](runners.md) — the execution backing for every instance.
- [Running a Configure](ansible-configure.md) — what happens once an Ansible-first instance has a configure to run: the triggers, the per-host report, the log.
- [Ansible inventory](ansible-inventory.md) — the structures an inventory-declaring module writes, and what ownership of a row means.
- [API Reference](api-reference.md#service-catalog) — full endpoint shapes.
- Original feature request: <https://github.com/mattrobinsonsre/terrapod/issues/535>
