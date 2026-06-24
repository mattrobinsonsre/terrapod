# Migrating a Terraform Platform onto Terrapod

Terrapod ships a CLI, `terrapod-migrate`, that moves a Terraform platform's
state and configuration onto a running Terrapod deployment. Two source
platforms are supported:

- **HCP Terraform / Terraform Enterprise (TFE)** — one TFE organization
  maps to one Terrapod deployment (Terrapod is single-org).
- **Atlantis** — `atlantis.yaml` v3 schema, or autodiscovery mode
  (no `atlantis.yaml` — use `--workspace` for direct per-workspace
  state migration); one or more repos map to Terrapod workspaces or
  autodiscovery rules.

The CLI is a Go binary distributed alongside the Terrapod provider on
every Terrapod release. Each release publishes:

- **macOS universal** — a single fat binary (lipo-merged darwin/amd64 +
  darwin/arm64) that runs natively on both Intel and Apple Silicon.
- **Linux** — amd64 + arm64.
- **Windows** — amd64 + arm64.

Each archive is accompanied by a SHA256 checksum file, and the checksum
file is detached-GPG-signed with the same key that signs the provider.

Releases pin to a Terrapod API version — the tool refuses to run against
a deployment whose version doesn't match, to keep schema drift from
producing half-migrated state.

> This page is the operator-facing runbook. Per-increment design rationale
> lives in code comments under `migrate/internal/`; that's where to start
> reading if you want to extend a source plugin.

## Status

Available since **v0.27.0**. The `apply`, `status`, `rewrite`, `verify`,
`cutover`, and `module` subcommands are fully implemented; `terrapod-migrate`
ships as a GitHub Release artifact (universal-macOS + linux/windows
amd64+arm64).

## Subcommands

- `terrapod-migrate apply` — read from `--source` (`tfe` | `atlantis`),
  write to `--target` Terrapod. Dry-run by default; pass `--apply` to
  actually write.
- `terrapod-migrate rewrite` — mechanically rewrite HCL `cloud {}` /
  `backend "remote"` blocks and private module sources in a local
  directory tree. No VCS interaction — operator commits and pushes after.
- `terrapod-migrate verify` — re-run plans on migrated workspaces to
  confirm parity with what the source produced.
- `terrapod-migrate status` — print the contents of the migration state
  file.

## The migration state file

`apply` reads and writes `./migration-state.json` (override with
`--state-file`). It records the SourceID → TerrapodID mapping for every
created resource, plus the source/destination hostnames and per-workspace
metadata the rewriter needs.

Re-running `apply` against the same state file is idempotent: previously
created resources are skipped. The `rewrite` subcommand can consume the
state file via `--state-file` (Mode 1) to derive the source/dest hosts
and the set of workspace names to rewrite — or operators can pass
`--source-host` / `--source-org` / `--dest-host` flags directly (Mode 2)
for ad-hoc rewriting without a migration record.

## Source: TFE / HCP Terraform

### What we migrate

- **Workspaces** — settings, tags → Terrapod labels, working directory,
  execution mode, VCS link, terraform/tofu version, resource sizing
- **State** — current state version per workspace, with serial + lineage
  preserved. For non-VCS workspaces, the latest uploaded configuration
  version tarball is migrated too (without it the migrated workspace has
  no code to run on first plan).
- **Variables and variable sets** — terraform + env, sensitive flag, HCL
  flag, varset scope. Sensitive values require an org-owner token to
  read; with a worker-tier token the tool emits a report of which
  variables the operator must re-enter post-migration.
- **Run triggers** — cross-workspace dependencies, when both endpoints
  are in the migration scope.
- **Notification configurations** — webhook, Slack, email; unsupported
  delivery types appear in the skipped-items report.
- **VCS connections** — one Terrapod connection per TFE oauth-client.
  GitHub and GitLab only; Bitbucket / Azure DevOps workspaces are
  migrated as CLI-driven (no VCS connection) with a skipped-items entry.
- **Private registry** — modules + module versions + providers + GPG
  signing keys. Tarballs and binaries are pulled from TFE and re-uploaded
  to Terrapod.
- **Agent pools** — pool names and workspace assignments. Tokens are not
  portable; the report lists each pool with a regenerate-token reminder.

### What we don't migrate (and why)

- **Sentinel policies** — proprietary to HashiCorp; Terrapod uses OPA
  via Rego. The skipped-items report lists every Sentinel policy
  attached to migrated workspaces by name so the operator can rewrite
  them as Rego under their own schedule.
- **Run history** — out of scope. Historic runs are too lossy to be
  useful and Terrapod treats the cutover as a clean line. The handover
  document records the last successful run per workspace as a reference.
- **Projects** — Terrapod is single-org with no project concept.
  Project tags are flattened onto workspace labels via the operator's
  `--project-label-key` mapping (default: `project`).
- **HCP Terraform Stacks** — out of scope.
- **Cost estimation history** — out of scope.
- **The `hashicorp/tfe` provider in your own IaC** — if you manage your
  TFE setup with HCL using the `tfe` provider, that whole module is
  broken post-migration: the `terrapod` provider has different attribute
  shapes (no projects, no teams, label-RBAC vs team-permissions). The
  tool emits `tfe-provider-references.md` listing every file using the
  `tfe` provider with a shape-change-per-resource summary — the actual
  rewrite is manual.

## Source: Atlantis

### What we migrate

- **Projects in `atlantis.yaml`** → Terrapod workspaces, or (when the
  pattern fits) a single autodiscovery rule covering them all.
- **Per-project settings** — `dir`, `workspace`, `terraform_version`,
  `autoplan` map to workspace `working-directory`, `terraform-version`,
  and autodiscovery rule fields.
- **VCS connection** — one Terrapod connection covering the source repos.

### What we don't migrate (and why)

- **Workflows** (multi-command pre/post steps) — Terrapod has no
  first-class equivalent. Recorded in the skipped-items report.
- **`apply_requirements`** (`approved`, `mergeable`, `undiverged`) — no
  direct Terrapod equivalent. Recorded as advisory metadata.
- **`terragrunt` projects** — the migration tool does not auto-translate
  terragrunt-driven Atlantis projects (their `terragrunt.hcl` dependency
  graphs and `generate` blocks aren't mechanically convertible), so they're
  detected and listed in the skipped-items report. This is a *migration-tool*
  limitation, not a runtime one: Terrapod itself runs Terragrunt in agent
  mode via the `terragrunt_enabled` workspace flag, so a skipped project can
  be re-created by hand. See [terragrunt.md](terragrunt.md).
- **PR comment history** — out of scope.

### Autodiscovery mode (no `atlantis.yaml`)

Many Atlantis deployments run in autodiscovery mode — Atlantis discovers
terraform directories automatically without an `atlantis.yaml` file. For
these setups, use the `--workspace` flag to target an existing Terrapod
workspace directly:

```
terrapod-migrate apply \
  --source=atlantis \
  --source-dir /path/to/terraform/project \
  --workspace my-workspace-name \
  --target https://terrapod.example.com \
  --apply
```

This bypasses `atlantis.yaml` parsing entirely. The tool detects the
backend from HCL in `--source-dir`, reads state from that backend, and
pushes it to the named Terrapod workspace. The workspace must already
exist (created via Terrapod's autodiscovery rules, the UI, or the API).

### State migration

Atlantis itself doesn't store state — the operator's HCL declares a
`terraform { backend "s3"/"gcs"/"azurerm"/"local" {} }` block, and the
state file lives in whichever bucket / blob container / disk path that
points at. Terrapod is single-backend on purpose — every workspace's
state lives in Terrapod's own storage; foreign backends aren't a
supported execution model.

Migration therefore reads each project's state from its declared
source-side location, pushes it into Terrapod, and rewrites the HCL to
replace the foreign `backend "..." {}` with `terraform { cloud { ... }
}` pointing at Terrapod. There is no "leave state in place" option.

Supported source-side backends (more can be added; these cover the
common cases):

- `s3` — AWS S3, reads via `aws-sdk-go-v2`, credentials via the SDK's
  default chain (env vars, `AWS_PROFILE`, IRSA, etc.). Lock-table
  configuration is read but not respected — the operator must pause
  Atlantis before running migration.
- `gcs` — Google Cloud Storage via `cloud.google.com/go/storage`,
  credentials via `GOOGLE_APPLICATION_CREDENTIALS` or `gcloud auth`.
- `azurerm` — Azure Blob Storage via `azure-sdk-for-go/sdk/storage/azblob`,
  credentials via `az login` / managed identity / `AZURE_*` env vars.
- `local` — file on disk inside the local clone (small dev setups only).

Workspaces using `backend "remote" { ... }` that points at TFE / HCP
are detected and rejected with a clear message — the operator should
run the migration with `--source=tfe` against the actual state holder.

State migration preserves serial + lineage so a re-run of
`terraform plan` against the migrated state matches what the source
produced.

## HCL rewriting

The `apply` subcommand writes to Terrapod's API; it does not edit the
operator's source repos. After `apply` succeeds, the operator runs:

```
terrapod-migrate rewrite --state-file migration-state.json --source-dir ~/code/api
```

against each repo (locally cloned, on the operator's own machine). The
tool walks the directory tree and mechanically rewrites:

- `terraform { cloud { hostname = "app.terraform.io", organization = "acme" ... } }` blocks → Terrapod hostname + `"default"` organization. Both `workspaces { name = "..." }` and `workspaces { tags = [...] }` forms are supported — only `hostname` and `organization` change; the workspace selection inside stays as-is (Terrapod's `tfe_v2` endpoint accepts the same `tags = [...]` syntax and translates internally). Tags are matched against workspace **labels**: a bare tag (`"core"`) matches any workspace with that label key, and a `key:value` tag (`"repo:web-app"`) matches that exact label. Use the colon form to select by key+value — OpenTofu rejects the Terraform 1.10+ map form (`tags = { repo = "..." }`), so `tags = ["repo:web-app"]` is the portable equivalent.
- `terraform { backend "remote" { hostname = "app.terraform.io", organization = "acme" ... } }` blocks → same destination as `cloud {}`.
- `source = "app.terraform.io/acme/<module>"` private-module references → `"<terrapod-host>/default/<module>"`.

The following are detected and listed but **not** rewritten because the
substitutions aren't mechanical:

- `provider "tfe" {}` declarations and `resource "tfe_*" {}` / `data
  "tfe_*" {}` blocks — different attribute shapes.

Module source rewriting is **opt-in** via `--rewrite-modules` on the
`rewrite` subcommand. Module pins are higher-blast-radius than backend
blocks (a working `source = "github.com/acme/vpc?ref=v1.0.0"` line is
easy to break with the wrong substitution) so the operator turns it on
consciously. When `--rewrite-modules` is set, the rewriter walks every
`module "..." { source = "..." }` block:

- **TFE-registry form** (`"app.terraform.io/<org>/<name>/<provider>"`)
  — looks up the matching Terrapod registry entry in the migration
  state file. If found, rewrites to the Terrapod coord. If missing,
  **hard error** by default (the operator just asked to rewrite and we
  can't fulfil it). `--allow-missing-module-mapping` downgrades to a
  warning.
- **Git form** (`"git::https://..."`) — looks up the matching `module
  register` record by git URL. Same hard-error-by-default behaviour.
- **Public registry form** (`"hashicorp/aws"`) — ignored, never
  rewritten.
- **Local path** (`"./modules/vpc"`) — ignored, never rewritten.

The Terrapod registry entries the rewriter looks up are created by
`apply --source=tfe` (for tarball-based migration of TFE's private
registry) and by the `module register` subcommand (for VCS-linked
modules). See "Module migration" below.

`rewrite` defaults to dry-run and prints a unified-diff report; pass
`--apply` to write files in place. The tool does not run `git` — the
operator inspects the diff, commits, and pushes via their normal flow.

## Module migration

Modules — like workspaces — are first-class migration targets. The
tool supports two subtypes:

### Tarball-based (TFE private registry → Terrapod private registry)

Pulls every version of every module from the source TFE org's private
registry, uploads to Terrapod's two-step module-version + tarball
upload endpoints. Full version history preserved. Runs automatically
as part of `apply --source=tfe` — no separate operator action.

Each migrated module gets a record in `migration-state.json` mapping
old source coordinate (`app.terraform.io/<org>/<name>/<provider>`) to
new Terrapod coordinate (`<terrapod-host>/default/<name>/<provider>`).
The `rewrite --rewrite-modules` step later consumes this record.

Providers and GPG signing keys travel with the modules.

### VCS-linked (`terrapod-migrate module register`)

For modules whose sources live in git (rather than in a private TFE
registry) the migration is a registration, not a tarball copy:
Terrapod's registry is told to watch the git repo's tag stream and
will publish versions over time as new tags appear. The operator
brings their own git URL.

Single module:

```bash
terrapod-migrate module register \
   --source-vcs https://github.com/acme/modules-vpc \
   --name vpc --provider aws --tag-prefix v \
   --target https://terrapod.acme.com \
   --apply
```

Batch via a config file (preferred for ≥10 modules — one source-
controlled file the team can review):

```bash
terrapod-migrate module register \
   --config-file modules.yaml \
   --target https://terrapod.acme.com \
   --apply
```

```yaml
# modules.yaml
modules:
  - source_vcs: https://github.com/acme/modules-vpc
    name: vpc
    provider: aws
    tag_prefix: v
  - source_vcs: https://github.com/acme/modules-eks
    name: eks
    provider: aws
    tag_prefix: v
```

Repeated single-shot invocations work too (idempotent thanks to the
migration state file) — useful for shell loops over an external list
of modules.

Each registered module gets a record in `migration-state.json` mapping
git URL → Terrapod coordinate, consumed by `rewrite --rewrite-modules`
when the operator opts in to rewriting `module "x" { source =
"git::..." }` references.

### Ordering invariant

Within `apply --source=tfe`, the call order is:

1. VCS connections
2. Workspaces (settings, vars, varsets)
3. **Private registry: modules + providers + GPG keys**
4. State (per workspace)
5. Run triggers, notifications, agent pools

Registry migration before state because the `rewrite` step (later)
needs the registry mapping in `migration-state.json` to rewrite
`source = "..."` lines. The operator runs `rewrite` after `apply`
completes.

## Cutover

`apply --lock-source` locks every TFE workspace before reading its state.
With the workspace locked, no in-flight TFE runs can change state under
the migration. The lock is released only by the cutover-handover step
(or by the operator manually if the migration aborts midway).

The handover document, written to `cutover-handover.md` at the end of an
`apply` run, lists:

- New Terrapod URLs per workspace.
- Every HCL change the operator needs to make (with file paths derived
  from TFE workspace `vcs-working-directory` + repo URL).
- Every skipped item with rationale.
- The state of any in-flight TFE runs at the moment of source-lock.
- A short checklist for the operator: rewrite HCL, redirect CI, retire
  TFE tokens.

## Pre-migration RBAC check

State files routinely contain secrets — database passwords, API keys,
cloud creds captured into outputs. If the destination Terrapod workspace
has broader read access than the source TFE workspace did, migration
silently widens who can read those secrets.

`apply` performs a pre-migration check: for each workspace, the tool
compares the source-side permission scope (TFE teams + workspace
permissions) against the destination's expected reachability (Terrapod
label-RBAC roles + assignments resolved through the planned mapping). If
the destination resolves looser, the tool warns and refuses to migrate
state for that workspace unless `--allow-rbac-widening` is set.

## Version match

The tool's build-time version must match the target Terrapod API's
reported version exactly (compared via `/.well-known/terraform.json`).
Mismatch refuses to run with a link to the matching release. Use
`--allow-api-version-mismatch` to bypass — useful for hotfixing within a
patch series but never recommended cross-minor.
