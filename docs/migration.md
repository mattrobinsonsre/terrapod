# Migrating a Terraform Platform onto Terrapod

Terrapod ships a CLI, `terrapod-migrate`, that moves a Terraform platform's
state and configuration onto a running Terrapod deployment. Two source
platforms are supported:

- **HCP Terraform / Terraform Enterprise (TFE)** — one TFE organization
  maps to one Terrapod deployment (Terrapod is single-org).
- **Atlantis** — `atlantis.yaml` v3 schema; one or more repos map to
  Terrapod workspaces or autodiscovery rules.

The CLI is a Go binary distributed alongside the Terrapod provider.
Releases pin to a Terrapod API version — the tool refuses to run against
a deployment whose version doesn't match, to keep schema drift from
producing half-migrated state.

> This page is the operator-facing runbook. Per-increment design rationale
> lives in code comments under `migrate/internal/`; that's where to start
> reading if you want to extend a source plugin.

## Status

The migration tool ships in **v0.27.0**. Until then, this page is a
forward-looking spec; the relevant subcommands are stubbed in the binary
with placeholders that exit non-zero.

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
- **`terragrunt` projects** — Terrapod doesn't run terragrunt. Detected
  and listed in the skipped-items report.
- **PR comment history** — out of scope.

### State strategy

Atlantis doesn't manage state — the operator already has `terraform {
backend "s3"/"gcs"/"azurerm" {} }` blocks declared. Two options:

- **Default — leave state in place.** The migrated Terrapod workspace
  runs in `agent` mode against the existing backend. The runner's
  backend-override file is *not* injected for these workspaces; they
  continue to use the operator's HCL-declared backend.
- **Opt-in — `--migrate-state`.** Rewrites the workspace's backend to
  `cloud { }`, runs `tofu init -migrate-state` once, and Terrapod owns
  the state afterwards. More invasive cutover but cleaner long-term.

## HCL rewriting

The `apply` subcommand writes to Terrapod's API; it does not edit the
operator's source repos. After `apply` succeeds, the operator runs:

```
terrapod-migrate rewrite --state-file migration-state.json --source-dir ~/code/api
```

against each repo (locally cloned, on the operator's own machine). The
tool walks the directory tree and mechanically rewrites:

- `terraform { cloud { hostname = "app.terraform.io", organization = "acme" ... } }` blocks → Terrapod hostname + `"default"` organization. Both `workspaces { name = "..." }` and `workspaces { tags = [...] }` forms are supported — only `hostname` and `organization` change; the workspace selection inside stays as-is (Terrapod's `tfe_v2` endpoint accepts the same `tags = [...]` syntax and translates internally).
- `terraform { backend "remote" { hostname = "app.terraform.io", organization = "acme" ... } }` blocks → same destination as `cloud {}`.
- `source = "app.terraform.io/acme/<module>"` private-module references → `"<terrapod-host>/default/<module>"`.

The following are detected and listed but **not** rewritten because the
substitutions aren't mechanical:

- `provider "tfe" {}` declarations and `resource "tfe_*" {}` / `data
  "tfe_*" {}` blocks — different attribute shapes.

`rewrite` defaults to dry-run and prints a unified-diff report; pass
`--apply` to write files in place. The tool does not run `git` — the
operator inspects the diff, commits, and pushes via their normal flow.

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
