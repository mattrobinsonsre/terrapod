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

> This page is the operator-facing runbook. Per-increment design rationale
> lives in code comments under `migrate/internal/`; that's where to start
> reading if you want to extend a source plugin.

## What actually transfers today

`terrapod-migrate` is built in increments. It is important to be precise
about what the tool **creates on Terrapod for you** versus what it
**reads, reports, and hands back to you as a checklist** — so nothing is
silently assumed done. The design principle is: auto-create the core that
gets a workspace planning again, surface everything else with
operator-readable guidance, and never pretend a resource migrated when it
didn't.

### Auto-created on Terrapod (the core)

| Resource | Detail |
|---|---|
| **VCS connections** | One Terrapod connection per source OAuth/PAT (TFE oauth-client, or one shared connection for Atlantis). GitHub and GitLab only. |
| **Workspaces** | Name, execution mode, terraform/tofu version, working directory, auto-apply, owner, VCS repo + branch link. TFE **tags → Terrapod labels** (`"k:v"` → `{k: "v"}`, bare `"k"` → `{k: ""}`). |
| **Workspace variables** | Terraform + env, sensitive flag, HCL flag, description. Sensitive values are read from the source **at apply time** and never written into the state file or dry-run report. |
| **State** | The current state version per workspace, with **serial + lineage preserved**, guarded against lineage mismatch and destination-serial-ahead (see [Reversibility](#reversibility-dry-run--rollback)). For **non-VCS** workspaces, the latest uploaded **configuration-version tarball** is migrated too (without it the workspace has no code to run on first plan). |
| **Variable sets** | Name, description, global/priority flags, their variables (sensitive values → empty + `sensitive=true` for re-entry, same as workspace variables), and workspace assignments — resolved to the migrated workspaces (created **after** workspaces so the assignment IDs are known). Assignments to workspaces outside the migration scope, and TFE project/stack scoping (no Terrapod equivalent), are reported for manual follow-up. |
| **Run triggers** | Cross-workspace dependencies (a source workspace's apply queues a run on the destination). Created after workspaces so both endpoints resolve to Terrapod IDs; a trigger is created only when **both** endpoints were migrated — one whose source or destination is outside the migration scope is reported for manual follow-up. |

### Read, reported, and left for you (a checklist, not yet auto-created)

The source plugin discovers these and lists them in the skipped-items
report + handover document with operator-readable guidance, but the tool
does **not** create them on Terrapod yet — you complete them by hand
(typically via `terraform-provider-terrapod`). They are on the roadmap as
later increments.

| Not-yet-created | How to complete it |
|---|---|
| **Notification configurations** | Reported per workspace (webhook / Slack / email). Recreate with `terrapod_notification_configuration`. |
| **Agent pools** | Reported by name + workspace assignments. Tokens are never portable — create the pool and regenerate a join token. |
| **Private registry** (modules, module versions, providers, GPG keys) | Reported for awareness. Republish with [`terrapod-publish`](registry-publishing.md), or point Terrapod's registry at the module's VCS tag stream. |
| **RBAC roles** | The tool generates a **suggested** role mapping from the source's teams/permissions into the handover doc. RBAC is the highest-blast-radius decision in a migration, so it is advisory only — you review, edit, and apply it via `terrapod_role` + `terrapod_role_assignment`. Nothing is applied automatically. |

### Not migrated by design (and why)

- **Sentinel policies** — proprietary to HashiCorp; Terrapod uses OPA/Rego.
  Every Sentinel policy attached to a migrated workspace is listed by name
  so you can rewrite it as Rego on your own schedule.
- **Run history** — out of scope; too lossy to be useful. The handover doc
  records the last successful run per workspace as a reference point.
- **Projects** — Terrapod is single-org with no project concept. Project
  membership is surfaced in the report; fold it onto workspace labels.
- **Teams as first-class objects** — replaced by label-RBAC (see the
  suggested-roles output above).
- **HCP Terraform Stacks** and **cost-estimation history** — out of scope.
- **The `hashicorp/tfe` provider in your own IaC** — if you manage your TFE
  setup with the `tfe` provider, that module doesn't carry over: the
  `terrapod` provider has different attribute shapes (no projects, no teams,
  label-RBAC vs team-permissions). Rewriting it is a manual exercise.

## Subcommands

`terrapod-migrate` has six subcommands:

- `terrapod-migrate apply` — read from `--source` (`tfe` | `atlantis`),
  write to `--target` Terrapod. **Dry-run by default**; pass `--apply` to
  actually write.
- `terrapod-migrate rewrite` — mechanically rewrite HCL `cloud {}` /
  `backend "remote"` blocks (hostname + organization) in a local directory
  tree. **Dry-run by default**; pass `--write` to touch disk. No VCS
  interaction — you commit and push after.
- `terrapod-migrate verify` — confirm each migrated workspace still matches
  what the migration recorded: it's present, the variable count is intact,
  and the state serial/lineage is unchanged. Exits non-zero on any mismatch.
- `terrapod-migrate rollback` — reverse a migration by deleting the
  workspaces it created. **Dry-run by default**; pass `--apply` to delete.
  Safe by construction (see [Reversibility](#reversibility-dry-run--rollback)).
- `terrapod-migrate status` — print the contents of the migration state file.
- `terrapod-migrate cutover` — lock/unlock the source TFE workspaces and
  write the handover document (TFE source only).

## Quickstart

### From TFE / HCP Terraform

```bash
# 1. Preview — writes nothing. Reports every workspace, variable, VCS
#    connection, and state version that WOULD be created, plus the
#    skipped-items checklist. Add --json for a machine-readable plan.
terrapod-migrate apply \
  --source tfe \
  --tfe-org example-org \
  --tfe-token "$TFE_TOKEN" \
  --target https://terrapod.example.com \
  --token "$TERRAPOD_TOKEN"

# 2. Commit — same command with --apply.
terrapod-migrate apply \
  --source tfe --tfe-org example-org --tfe-token "$TFE_TOKEN" \
  --target https://terrapod.example.com --token "$TERRAPOD_TOKEN" \
  --apply

# 3. Verify parity.
terrapod-migrate verify \
  --target https://terrapod.example.com --token "$TERRAPOD_TOKEN"

# 4. Rewrite each repo's cloud/backend block (locally cloned).
terrapod-migrate rewrite --dir ~/code/my-repo            # dry-run diff
terrapod-migrate rewrite --dir ~/code/my-repo --write    # write in place

# 5. Cut over: lock the source workspaces and generate the handover doc.
terrapod-migrate cutover \
  --tfe-org example-org --tfe-token "$TFE_TOKEN" \
  --lock --write-handover cutover-handover.md
```

### From Atlantis

```bash
# With an atlantis.yaml (projects → workspaces or an autodiscovery rule):
terrapod-migrate apply \
  --source atlantis \
  --source-dir ~/code/infra-repo \
  --target https://terrapod.example.com --token "$TERRAPOD_TOKEN" \
  --apply

# Autodiscovery mode (no atlantis.yaml) — push state for one existing
# Terrapod workspace directly from the repo's declared backend:
terrapod-migrate apply \
  --source atlantis \
  --source-dir ~/code/infra-repo/prod \
  --workspace prod-networking \
  --target https://terrapod.example.com --token "$TERRAPOD_TOKEN" \
  --apply
```

## Reversibility: dry-run + rollback

Reversibility is what makes a migration approvable — "we can undo it"
removes the largest switching-cost objection. The migration is therefore
**dry-run by default at both ends** and fully undoable.

**1. Preview with a dry-run.** `apply` writes nothing without `--apply`.
The dry-run reports exactly what *would* be created — every workspace,
variable, VCS-connection wiring, and **state version** (it reads the
source state to report its serial/lineage/size, but uploads nothing) —
plus the skipped-items report. Add `--json` for a machine-readable plan.

**2. Verify it landed.**

```bash
terrapod-migrate verify --target https://terrapod.example.com --token "$TERRAPOD_TOKEN"
```

**3. Roll back if it goes sideways.** `rollback` reads the state file and
deletes the workspaces the migration created (cascading their variables
and state) **plus the variable sets and run triggers it created** — run
triggers first, then variable sets, then workspaces (the reverse of the
create order). It is built to never destroy anything it shouldn't:

```bash
terrapod-migrate rollback --target https://terrapod.example.com --token "$TERRAPOD_TOKEN"           # dry-run: lists what would be deleted
terrapod-migrate rollback --target https://terrapod.example.com --token "$TERRAPOD_TOKEN" --apply   # delete
```

Safety guarantees:

- **Provenance gate** — deletes ONLY workspaces *this migration created*.
  Workspaces it merely reused (anything pre-existing, including
  `apply --workspace` direct targets) are never deleted.
- **Advanced-state guard** — before deleting, it checks the workspace's
  current state serial. If the workspace has been *used* since the
  migration (serial advanced past what was migrated), it is skipped;
  pass `--force` only if you really mean to discard that post-migration
  work. A destination it can't read is left alone (no blind deletes)
  unless `--force`.
- **VCS connections are never touched** — the migrator only ever matched
  pre-existing, operator-owned connections, so rollback leaves them in
  place.
- **Idempotent** — re-running is safe; already-deleted workspaces are
  recorded as rolled back and skipped.

So the end-to-end flow is "import in an afternoon, and roll back cleanly
if it doesn't go to plan."

## The migration state file

`apply` reads and writes `./migration-state.json` (override with
`--state-file`). It records the SourceID → TerrapodID mapping for every
created resource, plus the source/destination hostnames and per-workspace
metadata the rewriter needs.

Re-running `apply` against the same state file is idempotent: previously
created resources are skipped. The `rewrite` subcommand can consume the
state file via `--state-file` (Mode 1) to derive the source/dest hosts
and the set of workspace names to rewrite — or you can pass
`--source-host` / `--source-org` / `--dest-host` flags directly (Mode 2)
for ad-hoc rewriting without a migration record.

## Source: Atlantis

### What we read

- **Projects in `atlantis.yaml`** → Terrapod workspaces, or (when the
  pattern fits) a single autodiscovery rule covering them all.
- **Per-project settings** — `dir`, `workspace`, `terraform_version`,
  `autoplan` map to workspace `working-directory`, `terraform-version`,
  and autodiscovery rule fields.
- **VCS connection** — one Terrapod connection covering the source repos.

### What we don't read (and why)

- **Workflows** (multi-command pre/post steps) — Terrapod has no
  first-class equivalent. Recorded in the skipped-items report. (For custom
  pre/post steps, see [execution-hooks.md](execution-hooks.md).)
- **`apply_requirements`** (`approved`, `mergeable`, `undiverged`) — no
  direct Terrapod equivalent. Recorded as advisory metadata.
- **`terragrunt` projects** — not auto-translated (their `terragrunt.hcl`
  dependency graphs and `generate` blocks aren't mechanically convertible),
  so they're detected and listed in the skipped-items report. This is a
  *migration-tool* limitation, not a runtime one: Terrapod itself runs
  Terragrunt in agent mode via the `terragrunt_enabled` workspace flag, so a
  skipped project can be re-created by hand. See [terragrunt.md](terragrunt.md).
- **PR comment history** — out of scope.

### Autodiscovery mode (no `atlantis.yaml`)

Many Atlantis deployments run in autodiscovery mode — Atlantis discovers
terraform directories automatically without an `atlantis.yaml` file. For
these setups, use the `--workspace` flag to target an existing Terrapod
workspace directly:

```bash
terrapod-migrate apply \
  --source atlantis \
  --source-dir /path/to/terraform/project \
  --workspace my-workspace-name \
  --target https://terrapod.example.com --token "$TERRAPOD_TOKEN" \
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
source-side location, pushes it into Terrapod, and (via `rewrite`)
replaces the foreign `backend "..." {}` with `terraform { cloud { ... } }`
pointing at Terrapod. There is no "leave state in place" option.

Supported source-side backends (more can be added; these cover the
common cases):

- `s3` — AWS S3, reads via `aws-sdk-go-v2`, credentials via the SDK's
  default chain (env vars, `AWS_PROFILE`, IRSA, etc.). Lock-table
  configuration is read but not respected — pause Atlantis before running
  migration.
- `gcs` — Google Cloud Storage via `cloud.google.com/go/storage`,
  credentials via `GOOGLE_APPLICATION_CREDENTIALS` or `gcloud auth`.
- `azurerm` — Azure Blob Storage via `azure-sdk-for-go/sdk/storage/azblob`,
  credentials via `az login` / managed identity / `AZURE_*` env vars.
- `local` — file on disk inside the local clone (small dev setups only).

Workspaces using `backend "remote" { ... }` that points at TFE / HCP
are detected and rejected with a clear message — run the migration with
`--source=tfe` against the actual state holder instead.

State migration preserves serial + lineage so a re-run of
`terraform plan` against the migrated state matches what the source
produced.

## HCL rewriting

The `apply` subcommand writes to Terrapod's API; it does not edit your
source repos. After `apply` succeeds, run `rewrite` against each repo
(locally cloned, on your own machine):

```bash
terrapod-migrate rewrite --state-file migration-state.json --dir ~/code/my-repo
```

The tool walks the directory tree (recursing into `*.tf`, skipping
`.terraform` / `.git` / `node_modules`) and mechanically rewrites the
**backend declaration** — the hostname and organization only:

- `terraform { cloud { hostname = "app.terraform.io", organization = "acme" ... } }`
  → Terrapod hostname + `"default"` organization. Both
  `workspaces { name = "..." }` and `workspaces { tags = [...] }` forms are
  supported — only `hostname` and `organization` change; the workspace
  selection inside stays as-is. Tags are matched against workspace
  **labels**: a bare tag (`"core"`) matches any workspace with that label
  key, and a `key:value` tag (`"repo:web-app"`) matches that exact label.
  Use the colon form to select by key+value — OpenTofu rejects the
  Terraform 1.10+ map form (`tags = { repo = "..." }`), so
  `tags = ["repo:web-app"]` is the portable equivalent.
- `terraform { backend "remote" { hostname = "app.terraform.io", organization = "acme" ... } }`
  → same destination as `cloud {}`.

`rewrite` defaults to dry-run and prints a unified-diff report; pass
`--write` to write files in place. The tool does not run `git` — you
inspect the diff, commit, and push via your normal flow.

**Module sources are not rewritten.** If your configs reference private
modules by a source that changes under Terrapod (e.g. a TFE private-registry
coordinate), update those `source = "..."` lines by hand — module pins are
high-blast-radius and the tool leaves them to you deliberately.

## Cutover

The `cutover` subcommand (TFE source only) locks the source workspaces so
no in-flight TFE run can change state under the migration, and writes the
handover document:

```bash
terrapod-migrate cutover --tfe-org example-org --tfe-token "$TFE_TOKEN" \
  --lock --write-handover cutover-handover.md   # lock + handover
terrapod-migrate cutover --tfe-org example-org --tfe-token "$TFE_TOKEN" \
  --unlock                                       # release the locks
```

The handover document lists:

- New Terrapod URLs per workspace.
- The HCL changes you still need to make (backend blocks via `rewrite`;
  module sources by hand).
- Every skipped/checklist item with rationale — including the suggested
  RBAC role mapping (see below).
- The last successful run per workspace as a reference point.

## RBAC: suggested, never auto-applied

State files routinely contain secrets — database passwords, API keys,
cloud creds captured into outputs. If a destination Terrapod workspace has
broader read access than the source did, migration would silently widen who
can read those secrets.

Because that boundary is the highest-blast-radius decision in a migration,
`terrapod-migrate` does **not** apply RBAC automatically. Instead it
generates a **suggested** role mapping from the source's teams and
workspace permissions and writes it into the handover document as a
"suggested roles" section. You review, edit, and apply it via
`terrapod_role` + `terrapod_role_assignment` (the Terraform provider) — a
human signs off on every role boundary before it takes effect.
