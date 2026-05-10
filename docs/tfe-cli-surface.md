# TFE V2 CLI Surface

This file enumerates the **exact** set of TFE V2 API endpoints that the `terraform` (HashiCorp) and `tofu` (OpenTofu) CLIs consume — directly or via `go-tfe` — when configured with a `cloud` block (or the legacy `remote` backend) pointing at a TFE-compatible server.

**This list is the contract for `/api/v2/` in Terrapod.** A route on this list MUST be served at `/api/v2/...` so the CLI can find it via service discovery. A route NOT on this list — even one defined in the public TFE V2 spec — belongs at `/api/terrapod/v1/...`. The cleanup rule:

> If `terraform`/`tofu` doesn't call it, the route is Terrapod-native, regardless of TFE-V2 lineage.

Verification source: [OpenTofu](https://github.com/opentofu/opentofu) `internal/cloud` and `internal/backend/remote` packages, cross-referenced against [`go-tfe`](https://github.com/hashicorp/go-tfe) `client.NewRequest(...)` paths.

## Discovery / Auth

| Method | Path |
|---|---|
| GET | `/.well-known/terraform.json` |

The discovery document points at:
- `tfe.v2` — the TFE V2 API base, must remain `/api/v2/`
- `modules.v1` — module registry CLI download protocol
- `providers.v1` — provider registry CLI download protocol
- `login.v1` — `terraform login` OAuth2 endpoints (`/oauth/authorize`, `/oauth/token`)

## Organizations

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/v2/ping` | client init | `go-tfe` configures every connection |
| GET | `/api/v2/account/details` | `Account.Read` | `go-tfe` startup |
| GET | `/api/v2/organizations/default` | `Organizations.Read` | cloud backend init |
| GET | `/api/v2/organizations/default/entitlement-set` | `Organizations.ReadEntitlements` | `cloud/backend.go:349`, `remote/backend.go:362` |
| GET | `/api/v2/organizations/default/runs/queue` | `Organizations.ReadRunQueue` | `cloud/backend_common.go:170`, `remote/backend_common.go:172` (run-status display) |
| GET | `/api/v2/organizations/default/capacity` | `Organizations.ReadCapacity` | `cloud/backend_common.go:193`, `remote/backend_common.go:195` |

## Projects

`cloud` block only — the `project` argument routes workspaces into projects.

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/v2/organizations/default/projects` | `Projects.List` | `cloud/backend.go:588, 676` |
| POST | `/api/v2/organizations/default/projects` | `Projects.Create` | `cloud/backend.go:715` |

Terrapod is single-organization with no project concept — see #279 for current handling.

## Workspaces

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/v2/organizations/default/workspaces` | `Workspaces.List` | `cloud/backend.go:601, 1175`, `remote/backend.go:488` |
| POST | `/api/v2/organizations/default/workspaces` | `Workspaces.Create` | `cloud/backend.go:726`, `remote/backend.go:591` |
| GET | `/api/v2/organizations/default/workspaces/{name}` | `Workspaces.Read` | `cloud/backend.go:635, 661, 1120, 1151`, `cloud/backend_common.go:95`, `cloud/backend_context.go:178`, `remote/backend.go:575, 643`, `remote/backend_context.go:181` |
| DELETE | `/api/v2/organizations/default/workspaces/{name}` | `Workspaces.Delete` | `remote/backend_state.go:145` |
| GET | `/api/v2/workspaces/{id}` | (read by id) | go-tfe pattern |
| PATCH | `/api/v2/workspaces/{id}` | `Workspaces.UpdateByID` | `cloud/backend.go:739` |
| POST | `/api/v2/workspaces/{id}/relationships/tags` | `Workspaces.AddTags` | `cloud/backend.go:760` |
| DELETE | `/api/v2/workspaces/{id}/relationships/tags` | `Workspaces.RemoveTags` | tag-binding maintenance |
| GET | `/api/v2/workspaces/{id}/tag-bindings` | `Workspaces.ListTagBindings` | tag display |
| GET | `/api/v2/workspaces/{id}/effective-tag-bindings` | `Workspaces.ListEffectiveTagBindings` | tag display |
| POST | `/api/v2/workspaces/{id}/actions/lock` | `Workspaces.Lock` | `remote/backend_state.go:164` |
| POST | `/api/v2/workspaces/{id}/actions/unlock` | `Workspaces.Unlock` | `remote/backend_state.go:201` |
| POST | `/api/v2/workspaces/{id}/actions/force-unlock` | `Workspaces.ForceUnlock` | `remote/backend_state.go:222` (gap — see #279) |
| GET | `/api/v2/workspaces/{id}/runs` | `Runs.List` | `cloud/backend_common.go:119`, `remote/backend_common.go:121` |
| GET | `/api/v2/workspaces/{id}/vars` | `Variables.List` | `cloud/backend_context.go:122`, `remote/backend_context.go:123` (read-only — sensitive-var warnings) |

Note: `DELETE /api/v2/workspaces/{id}` is **not** in the CLI surface. Only the by-name delete is. The by-id delete is admin/UI-only and lives on the management API.

## State Versions

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/v2/workspaces/{id}/current-state-version` | `StateVersions.ReadCurrent` | `remote/backend_state.go:40` |
| POST | `/api/v2/workspaces/{id}/state-versions` | `StateVersions.Create` | `remote/backend_state.go:85` |
| GET | `/api/v2/state-versions/{id}` | `StateVersions.Read` | follow-up reads |
| PUT | `/api/v2/state-versions/{id}/content` | (raw upload to `upload-url`) | `remote/backend_state.go:129` — **no Authorization header** |
| PUT | `/api/v2/state-versions/{id}/json-content` | (raw upload) | same flow, JSON state |
| GET | `/api/v2/state-versions/{id}/download` | `StateVersions.Download` | `remote/backend_state.go:49` (follows `download-url` from resource) |

The `upload-url` and `download-url` returned in JSON:API attributes can be absolute or server-relative — go-tfe's `NewRequest` handles both. Terrapod returns relative `/api/v2/...` paths.

## Configuration Versions

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| POST | `/api/v2/workspaces/{id}/configuration-versions` | `ConfigurationVersions.Create` | `cloud/backend_plan.go:135`, `remote/backend_plan.go:206` |
| GET | `/api/v2/configuration-versions/{id}` | `ConfigurationVersions.Read` | `cloud/backend_plan.go:199`, `remote/backend_plan.go:270` |
| PUT | `/api/v2/configuration-versions/{id}/upload` | (raw upload to `upload-url`) | `cloud/backend_plan.go:186`, `remote/backend_plan.go:257` — **no Authorization header** |

Terrapod-only management on the configuration-versions surface (list, download, diff, ticket-based download) lives at `/api/terrapod/v1/configuration-versions/...`.

## Runs / Plans / Applies

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| POST | `/api/v2/runs` | `Runs.Create` | `cloud/backend_plan.go:278`, `remote/backend_plan.go:323` |
| GET | `/api/v2/runs/{id}` | `Runs.Read` / `ReadWithOptions` | many — every run-status poll |
| POST | `/api/v2/runs/{id}/actions/apply` | `Runs.Apply` | `cloud/backend_apply.go:197`, `remote/backend_apply.go:253` |
| POST | `/api/v2/runs/{id}/actions/discard` | `Runs.Discard` | `cloud/backend_common.go:519`, `remote/backend_apply.go:208` |
| POST | `/api/v2/runs/{id}/actions/cancel` | `Runs.Cancel` | `cloud/backend.go:934`, `remote/backend.go:818` |
| GET | `/api/v2/runs/{id}/run-events` | `RunEvents.List` | run progress polling |
| GET | `/api/v2/plans/{id}` | `Plans.Read` | run status |
| GET | `/api/v2/plans/{id}/log` | `Plans.Logs` (via `log-read-url`) | `cloud/backend_plan.go:428`, `remote/backend_plan.go:380` |
| GET | `/api/v2/plans/{id}/json-output` | `Plans.ReadJSONOutput` | `cloud/backend_show.go:68` (302 → presigned storage URL; advertised via `json-output` attribute on the plan when present) |
| GET | `/api/v2/applies/{id}` | `Applies.Read` | run status |
| GET | `/api/v2/applies/{id}/log` | `Applies.Logs` (via `log-read-url`) | `cloud/backend_apply.go:229`, `remote/backend_apply.go:272` |

## Cost Estimates / Policy Checks / Task Stages

These are CLI-aware (run progress display branches on relationships) but only exercised when the relationship is present on the run. Terrapod intentionally does not implement cost estimates or Sentinel policy checks (see CLAUDE.md "Out of Scope"). Run tasks ARE supported, with task stages on the CLI surface:

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/v2/task-stages/{id}` | `TaskStages.Read` | `cloud/backend_taskStages.go:66, 96` |
| POST | `/api/v2/task-stages/{id}/actions/override` | `TaskStages.Override` | `cloud/backend_taskStages.go:186` |

The run-task management surface (`/run-tasks/*`, `/workspaces/{id}/run-tasks`, callback endpoints) is Terrapod-native and lives at `/api/terrapod/v1/`.

## tfci / tfc-workflows-github

[`tfci`](https://github.com/hashicorp/tfc-workflows-tooling) is HashiCorp's CI binary; [`tfc-workflows-github`](https://github.com/hashicorp/tfc-workflows-github) wraps it for GitHub Actions. It is in widespread use for TFE/HCP-Terraform CI flows. Most of what it calls overlaps with the CLI surface above (workspace lookup, lock/unlock, configuration-version create+upload, run create/apply/discard/cancel/read, plan read+log+JSON output). The one extension beyond the `terraform`/`tofu` CLI surface is **variable management** — `tfci variable …` and `tfci variable-set …` commands.

We extend the "stays at `/api/v2/`" set to cover those calls. The rule is unchanged: anything `terraform`, `tofu`, or `tfci` calls stays at `/api/v2/`; everything else is `/api/terrapod/v1/`.

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| POST | `/api/v2/workspaces/{id}/vars` | `Variables.Create` | `tfci variable create` |
| PATCH | `/api/v2/workspaces/{id}/vars/{id}` | `Variables.Update` | `tfci variable update` |
| DELETE | `/api/v2/workspaces/{id}/vars/{id}` | `Variables.Delete` | `tfci variable delete` |
| GET | `/api/v2/organizations/default/varsets` | `VariableSets.List` | `tfci variable-set list` |
| POST | `/api/v2/organizations/default/varsets` | `VariableSets.Create` | `tfci variable-set create` |
| GET | `/api/v2/varsets/{id}` | `VariableSets.Read` | `tfci variable-set show` |
| PATCH | `/api/v2/varsets/{id}` | `VariableSets.Update` | `tfci variable-set update` |
| DELETE | `/api/v2/varsets/{id}` | `VariableSets.Delete` | `tfci variable-set delete` |
| GET | `/api/v2/varsets/{id}/relationships/vars` | `VariableSetVariables.List` | `tfci variable-set ...` |
| POST | `/api/v2/varsets/{id}/relationships/vars` | `VariableSetVariables.Create` | `tfci variable-set add-variable` |
| PATCH | `/api/v2/varsets/{id}/relationships/vars/{id}` | `VariableSetVariables.Update` | `tfci variable-set update-variable` |
| DELETE | `/api/v2/varsets/{id}/relationships/vars/{id}` | `VariableSetVariables.Delete` | `tfci variable-set delete-variable` |
| POST | `/api/v2/varsets/{id}/relationships/workspaces` | `VariableSets.ApplyToWorkspaces` | `tfci variable-set assign-to-workspace` |
| DELETE | `/api/v2/varsets/{id}/relationships/workspaces` | `VariableSets.RemoveFromWorkspaces` | `tfci variable-set remove-from-workspace` |

**Verification:** When updating this section, check the `tfci` source at https://github.com/hashicorp/tfc-workflows-tooling — the `internal/cloud/` package exposes the call sites.

**Out of scope for tfci compat:** teams, projects, policy checks, run tasks (TFE-shape), notifications, OAuth client management, the `hashicorp/tfe` Terraform provider's full surface. Those have structural divergence in Terrapod (single-org, label-RBAC instead of teams) that compatibility cannot bridge.

## Module Registry (CLI Download Protocol)

Service-discovered via `modules.v1` URL.

| Method | Path |
|---|---|
| GET | `/api/v2/registry/modules/{namespace}/{name}/{provider}/versions` |
| GET | `/api/v2/registry/modules/{namespace}/{name}/{provider}/{version}/download` |

The download endpoint returns 204 with `X-Terraform-Get` header pointing at the actual tarball URL.

## Provider Registry (CLI Download Protocol)

Service-discovered via `providers.v1` URL.

| Method | Path |
|---|---|
| GET | `/api/v2/registry/providers/{namespace}/{type}/versions` |
| GET | `/api/v2/registry/providers/{namespace}/{type}/{version}/download/{os}/{arch}` |

The platform-download endpoint returns JSON with `download_url`, `shasums_url`, and `shasums_signature_url` — the CLI then follows those URLs (which may be absolute, presigned, or server-relative).

## Maintaining This Document

When OpenTofu adds new go-tfe call sites in `internal/cloud/*` or `internal/backend/remote/*`, this list grows. Re-verify by:
1. `git clone https://github.com/opentofu/opentofu`
2. `grep -RP 'tfe\.[A-Z]\w+\(' internal/cloud internal/backend/remote --include='*.go' | grep -v _test.go`
3. For each match, find the go-tfe method's `client.NewRequest(...)` line in https://github.com/hashicorp/go-tfe to extract the path
4. Add to the relevant table above

Endpoints **not** verified by this process belong at `/api/terrapod/v1/`, regardless of go-tfe lineage.
