# TFE V2 CLI Surface

This file enumerates the **exact** set of TFE V2 API endpoints that the `terraform` (HashiCorp) and `tofu` (OpenTofu) CLIs consume — directly or via `go-tfe` — when configured with a `cloud` block (or the legacy `remote` backend) pointing at a TFE-compatible server.

**This list is the contract for `/api/tfe/v2/` in Terrapod.** A route on this list MUST be served at `/api/tfe/v2/...` so the CLI can find it via service discovery. A route NOT on this list — even one defined in the public TFE V2 spec — belongs at `/api/v1/...`. The cleanup rule:

> If `terraform`/`tofu` doesn't call it, the route is Terrapod-native, regardless of TFE-V2 lineage.

Verification source: [OpenTofu](https://github.com/opentofu/opentofu) `internal/cloud` and `internal/backend/remote` packages, cross-referenced against [`go-tfe`](https://github.com/hashicorp/go-tfe) `client.NewRequest(...)` paths.

## Discovery / Auth

| Method | Path |
|---|---|
| GET | `/.well-known/terraform.json` |

The discovery document points at:
- `tfe.v2` — the TFE V2 API base. **Discovered, not fixed**: `terraform` and
  `tofu` both honour whatever path is advertised here (verified with a real
  `init`/`plan`/`apply` in #1528), which is what allowed this surface to move off
  `/api/v2`. The old path is still served for clients that cache discovery.
- `modules.v1` — module registry CLI download protocol
- `providers.v1` — provider registry CLI download protocol
- `login.v1` — `terraform login` OAuth2 endpoints (`/oauth/authorize`, `/oauth/token`)

## Organizations

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/tfe/v2/ping` | client init | `go-tfe` configures every connection |
| GET | `/api/tfe/v2/account/details` | `Account.Read` | `go-tfe` startup |
| GET | `/api/tfe/v2/organizations/default` | `Organizations.Read` | cloud backend init |
| GET | `/api/tfe/v2/organizations/default/entitlement-set` | `Organizations.ReadEntitlements` | `cloud/backend.go:349`, `remote/backend.go:362` |
| GET | `/api/tfe/v2/organizations/default/runs/queue` | `Organizations.ReadRunQueue` | `cloud/backend_common.go:170`, `remote/backend_common.go:172` (run-status display) — **NOT SERVED, see #2378** |
| GET | `/api/tfe/v2/organizations/default/capacity` | `Organizations.ReadCapacity` | `cloud/backend_common.go:193`, `remote/backend_common.go:195` — **NOT SERVED, see #2378** |

> Those last two 404 today. They are unreachable rather than broken, because
> `waitForRun` returns early unless the run is `pending`/`confirmed` and Terrapod
> reports `queued` — so **adding them is a prerequisite for matching the run-status
> vocabulary, not an independent tidy-up.** #2378 has the detail.

## Projects

`cloud` block only — the `project` argument routes workspaces into projects.

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/tfe/v2/organizations/default/projects` | `Projects.List` | `cloud/backend.go:588, 676` (returns 422 — see below) |
| POST | `/api/tfe/v2/organizations/default/projects` | `Projects.Create` | `cloud/backend.go:715` (returns 422 — see below) |

Terrapod is single-organization with no project concept. Both endpoints return `422 Projects are not supported` with a JSON:API error directing the caller to omit the `project` argument from the cloud block. See `docs/getting-started.md`. Label-based RBAC (`docs/rbac.md`) covers the same scoping use case projects served in TFC, and is more flexible — a workspace can match on any combination of label dimensions instead of a single project, and the same labels drive UI filtering of workspaces and other resources.

## Workspaces

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/tfe/v2/organizations/default/workspaces` | `Workspaces.List` | `cloud/backend.go:601, 1175`, `remote/backend.go:488` |
| POST | `/api/tfe/v2/organizations/default/workspaces` | `Workspaces.Create` | `cloud/backend.go:726`, `remote/backend.go:591` |
| GET | `/api/tfe/v2/organizations/default/workspaces/{name}` | `Workspaces.Read` | `cloud/backend.go:635, 661, 1120, 1151`, `cloud/backend_common.go:95`, `cloud/backend_context.go:178`, `remote/backend.go:575, 643`, `remote/backend_context.go:181` |
| DELETE | `/api/tfe/v2/organizations/default/workspaces/{name}` | `Workspaces.Delete` | `remote/backend_state.go:145` — **NOT SERVED** |
| GET | `/api/tfe/v2/workspaces/{id}` | (read by id) | go-tfe pattern |
| PATCH | `/api/tfe/v2/workspaces/{id}` | `Workspaces.UpdateByID` | `cloud/backend.go:739` |
| POST | `/api/tfe/v2/workspaces/{id}/relationships/tags` | `Workspaces.AddTags` | `cloud/backend.go:760` |
| DELETE | `/api/tfe/v2/workspaces/{id}/relationships/tags` | `Workspaces.RemoveTags` | tag-binding maintenance |
| GET | `/api/tfe/v2/workspaces/{id}/tag-bindings` | `Workspaces.ListTagBindings` | tag display |
| GET | `/api/tfe/v2/workspaces/{id}/effective-tag-bindings` | `Workspaces.ListEffectiveTagBindings` | tag display |
| POST | `/api/tfe/v2/workspaces/{id}/actions/lock` | `Workspaces.Lock` | `remote/backend_state.go:164` |
| POST | `/api/tfe/v2/workspaces/{id}/actions/unlock` | `Workspaces.Unlock` | `remote/backend_state.go:201` |
| POST | `/api/tfe/v2/workspaces/{id}/actions/force-unlock` | `Workspaces.ForceUnlock` | `remote/backend_state.go:222` |
| GET | `/api/tfe/v2/workspaces/{id}/runs` | `Runs.List` | `cloud/backend_common.go:119`, `remote/backend_common.go:121` |
| GET | `/api/tfe/v2/workspaces/{id}/vars` | `Variables.List` | `cloud/backend_context.go:122`, `remote/backend_context.go:123` (read-only — sensitive-var warnings) |

Note: `DELETE /api/tfe/v2/workspaces/{id}` is **not** in the CLI surface — the by-id
delete is admin/UI-only and lives on the management API.

The by-name delete is **not served either, and will not be** — deleting a workspace
from the CLI is deliberately unsupported (decided 2026-11-04). Workspace lifecycle
is a management operation and lives on the native API and the UI, where it is
RBAC-gated and audited; a `cloud {}` block is for running Terraform, not for
destroying the thing holding your state.

So the row above records what the CLI **calls**, not what Terrapod serves.
`terraform/tofu workspace delete` fails, and the mechanism is worth knowing because
it is not obvious: `permissions.can-force-delete` is absent from the workspace
document, `go-tfe` types it `*bool` precisely so nil means "server predates safe
delete", and `internal/cloud/state.go` then sends **every** delete down
`Workspaces.Delete(org, name)`. Supporting it would therefore mean adding three
things together — that route, `can-force-delete`, and `actions/safe-delete` — since
emitting the permission alone just moves the failure to the endpoint it then
selects. Three routes to serve one verb we have chosen not to expose is the wrong
trade; see `docs/known-limitations.md`.

## State Versions

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/tfe/v2/workspaces/{id}/current-state-version` | `StateVersions.ReadCurrent` | `remote/backend_state.go:40` |
| POST | `/api/tfe/v2/workspaces/{id}/state-versions` | `StateVersions.Create` | `remote/backend_state.go:85` |
| GET | `/api/tfe/v2/state-versions/{id}` | `StateVersions.Read` | follow-up reads |
| PUT | `/api/tfe/v2/state-versions/{id}/content` | (raw upload to `upload-url`) | `remote/backend_state.go:129` — **no Authorization header** |
| PUT | `/api/tfe/v2/state-versions/{id}/json-content` | (raw upload) | same flow, JSON state |
| GET | `/api/tfe/v2/state-versions/{id}/download` | `StateVersions.Download` | `remote/backend_state.go:49` (follows `download-url` from resource) |
| GET | `/api/tfe/v2/workspaces/{id}/current-state-version-outputs` | `StateVersionOutputs.ReadCurrent` | `cloud/state.go` `GetRootOutputValues` — **`tofu output` / `terraform output`** |
| GET | `/api/tfe/v2/state-version-outputs/{id}` | `StateVersionOutputs.Read` | same, once per **sensitive** output |

The `upload-url` and `download-url` returned in JSON:API attributes **must be
absolute, and Terrapod returns absolute URLs.** Do not "simplify" them to
server-relative paths: an upload does not go through `go-tfe`'s `NewRequest` at
all. `Client.doForeignPUTRequest` does `url.Parse(foreignURL)` and PUTs that
verbatim with no Authorization header, so a relative path has no host to resolve
against. (This paragraph previously said the opposite on both counts, which is the
shape of the #1703 regression.)

The two **output** routes are a pair and the split between them is deliberate.
`GetRootOutputValues` calls `ReadCurrent` first and then `Read` **once per
sensitive output**, so the collection withholds a sensitive value and the
per-output route reveals it. That lets the collection gate on
`state:read-metadata` (the read tier) while the reveal needs `state:read` (the
plan tier, GHSA-gwwq-5v7q-h3f4) — so a read-only user gets `tofu output` and
meets a refusal only on an output that is genuinely secret. The collection also
honours the runner allowlist (#344) that the sibling `current-state-version`
route does, because a run permitted to read a producer's whole state may read
its outputs.

Two things about them that are easy to get wrong. **Every output must carry a
non-nil `detailed-type`**: `GetRootOutputValues` falls back to reading full
state the moment one is nil, so an empty or stubbed document silently reports
*no* outputs rather than failing. And the **id is synthesised** —
`wsout-{state-version-id}:{name}` — because outputs are re-derived from the
state blob and no row exists; go-tfe's parser drops an entry without an id, so
the CLI would conclude the workspace has none.

They are TFE-only rather than dual-mounted: the document carries a cty
`detailed-type` and a Pulumi stack's outputs have no type to put in one. The
native `GET /api/v1/workspaces/{id}/state-outputs` serves every engine, with
sensitive values masked.

`GET /api/tfe/v2/workspaces/{id}/state-versions` (the list) is also served, on both
surfaces, though no CLI calls it.

## Configuration Versions

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| POST | `/api/tfe/v2/workspaces/{id}/configuration-versions` | `ConfigurationVersions.Create` | `cloud/backend_plan.go:135`, `remote/backend_plan.go:206` |
| GET | `/api/tfe/v2/configuration-versions/{id}` | `ConfigurationVersions.Read` | `cloud/backend_plan.go:199`, `remote/backend_plan.go:270` |
| PUT | `/api/tfe/v2/configuration-versions/{id}/upload` | (raw upload to `upload-url`) | `cloud/backend_plan.go:186`, `remote/backend_plan.go:257` — **no Authorization header** |

`GET /api/tfe/v2/workspaces/{id}/configuration-versions` (the list) is served too,
though no CLI calls it. Other Terrapod-only management on this surface (download,
diff, ticket-based download) lives at `/api/v1/configuration-versions/...`.

## Runs / Plans / Applies

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| POST | `/api/tfe/v2/runs` | `Runs.Create` | `cloud/backend_plan.go:278`, `remote/backend_plan.go:323` |
| GET | `/api/tfe/v2/runs/{id}` | `Runs.Read` / `ReadWithOptions` | many — every run-status poll |
| POST | `/api/tfe/v2/runs/{id}/actions/apply` | `Runs.Apply` | `cloud/backend_apply.go:197`, `remote/backend_apply.go:253` |
| POST | `/api/tfe/v2/runs/{id}/actions/discard` | `Runs.Discard` | `cloud/backend_common.go:519`, `remote/backend_apply.go:208` |
| POST | `/api/tfe/v2/runs/{id}/actions/cancel` | `Runs.Cancel` | `cloud/backend.go:934`, `remote/backend.go:818` |
| GET | `/api/tfe/v2/runs/{id}/run-events` | `RunEvents.List` | not called by the CLI; served for go-tfe clients |
| GET | `/api/tfe/v2/plans/{id}` | `Plans.Read` | run status |
| GET | `/api/tfe/v2/plans/{id}/log` | `Plans.Logs` (via `log-read-url`) | `cloud/backend_plan.go:428`, `remote/backend_plan.go:380` |
| GET | `/api/tfe/v2/plans/{id}/json-output` | `Plans.ReadJSONOutput` | `cloud/backend_show.go:68` (302 → presigned storage URL; advertised via `json-output` attribute on the plan when present) |
| GET | `/api/tfe/v2/applies/{id}` | `Applies.Read` | run status |
| GET | `/api/tfe/v2/applies/{id}/log` | `Applies.Logs` (via `log-read-url`) | `cloud/backend_apply.go:229`, `remote/backend_apply.go:272` |

### Run-create attributes

The tables above catalogue **routes**, and that is what decides whether a route
belongs on the compatibility surface. It is not sufficient on its own: a route
can be served correctly while an attribute the CLI sends on it is dropped, and
nothing here would show it. That is exactly how saved-plan runs were missed
(#1903) — `POST /api/tfe/v2/runs` was listed and implemented, and `save-plan`
was read off the body by nobody.

So the body of the one route the CLI *writes* to is catalogued too. Every
attribute `go-tfe`'s `RunCreateOptions` defines that Terrapod consumes:

| Attribute | go-tfe field | Terrapod behaviour |
|---|---|---|
| `plan-only` | `PlanOnly` | Speculative run; never applies. Also forced on by a speculative configuration version, a `vcs-ref` override, and a CLI-initiated run on a VCS-connected agent workspace |
| `save-plan` | `SavePlan` | `terraform plan -out=FILE`. Apply-capable, apply **deferred**: plans immediately without taking the workspace's apply slot, takes it on confirm. Refused alongside `plan-only`, alongside a speculative configuration version, and on a VCS-connected agent workspace |
| `is-destroy` | `IsDestroy` | `terraform destroy`; needs `run:apply-destroy` |
| `message` | `Message` | Recorded on the run |
| `auto-apply` | `AutoApply` | Overrides the workspace setting for this run |
| `refresh` | `Refresh` | `-refresh=false` |
| `refresh-only` | `RefreshOnly` | `-refresh-only` |
| `target-addrs` | `TargetAddrs` | `-target` |
| `replace-addrs` | `ReplaceAddrs` | `-replace` |
| `allow-empty-apply` | `AllowEmptyApply` | `-allow-empty-apply` |
| `terraform-version` / `engine-version` | `TerraformVersion` | Pins the engine version for this run (#1559) |
| `variables` | `Variables` | `-var` / `-var-file` for THIS run (#2377). **Each `value` is an HCL expression, not a literal** — `-var msg=x` arrives as `"x"`, quotes included, which is what lets `-var 'l=["a"]'` work. Higher precedence than the workspace's own variables; a key pinned by a **priority** variable set is refused with a 422 naming it rather than silently dropped. Terraform/OpenTofu only, because an HCL expression has no meaning to Pulumi — a truthy value on a Pulumi workspace is refused 422. An empty array is a no-op, which matters because the CLI sends the key on every run. Stored encrypted and never echoed back; the run document reports `run-variable-keys`, the keys alone |

Terrapod-native attributes on the same route (`vcs-ref`, `is-drift-detection`)
are not part of this contract and are documented in
[api-reference.md](api-reference.md).

**An attribute `go-tfe` defines and Terrapod does not consume must be listed
here as unsupported, or refused — never accepted and ignored.** Accepting it
turns a documented CLI workflow into a silent surprise, which is worse than
either supporting it or saying plainly that it is not supported.

Under that rule, these `RunCreateOptions` fields are **accepted and not consumed**:

| Attribute | go-tfe field | Status |
|---|---|---|
| `allow-config-generation` | `AllowConfigGeneration` | Not consumed; `tofu plan -generate-config-out` produces nothing |
| `policy-paths` | `PolicyPaths` | Not consumed (BETA upstream; neither OpenTofu backend sends it) |
| `invoke-action-addrs` | `InvokeActionAddrs` | Not consumed; OpenTofu has no actions feature, so nothing sends it today |

`debugging-mode` appears in HashiCorp's published request table but in no `go-tfe`
`RunCreateOptions` struct, and no client we can find sends it.

## Cost Estimates / Policy Checks / Task Stages

These are CLI-aware (run progress display branches on relationships) but only exercised when the relationship is present on the run. Terrapod does not implement cost estimates in this shape, or Sentinel. Its OPA policy sets and security scan are served as **policy checks**, and run tasks as **task stages** (#1704).

The run lists both in its relationships only in the Terraform Enterprise post-plan vocabulary (`api.config.runs.tfe_post_plan_decisions`, the default from 2.0), because that is what makes the CLI act on them; the same setting makes a held run report `policy_override` / `post_plan_awaiting_decision`, the statuses on which the CLI's confirm loop offers an override. See [post-plan-decisions.md](post-plan-decisions.md).

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| GET | `/api/tfe/v2/runs/{id}?include=task_stages` | `Runs.ReadWithOptions` | `cloud/backend_taskStages.go:42` |
| GET | `/api/tfe/v2/task-stages/{id}` | `TaskStages.Read` | `cloud/backend_taskStages.go:66, 96` |
| POST | `/api/tfe/v2/task-stages/{id}/actions/override` | `TaskStages.Override` | `cloud/backend_taskStages.go:186` |
| GET | `/api/tfe/v2/policy-checks/{id}` | `PolicyChecks.Read` | `cloud/backend_common.go:326` |
| GET | `/api/tfe/v2/policy-checks/{id}/output` | `PolicyChecks.Logs` | `cloud/backend_common.go:319` |
| POST | `/api/tfe/v2/policy-checks/{id}/actions/override` | `PolicyChecks.Override` | `cloud/backend_common.go:394, 411` |
| GET | `/api/tfe/v2/runs/{id}/policy-checks` | `PolicyChecks.List` | not called by the `terraform`/`tofu` CLI; **`tfci run create` does call it** (`GetPolicyCheckLogs`) |

The run-task management surface (`/run-tasks/*`, `/workspaces/{id}/run-tasks`, callback endpoints) is Terrapod-native and lives at `/api/v1/`.

## tfci / tfc-workflows-github

[`tfci`](https://github.com/hashicorp/tfc-workflows-tooling) is HashiCorp's CI binary; [`tfc-workflows-github`](https://github.com/hashicorp/tfc-workflows-github) wraps it for GitHub Actions. It is in widespread use for TFE/HCP-Terraform CI flows. Most of what it calls overlaps with the CLI surface above (workspace lookup, lock/unlock, configuration-version create+upload, run create/apply/discard/cancel/read, plan read+log+JSON output).

**`tfci` has exactly eight commands, and variable management is not among them.** Its
own command map in `cli.go` registers `plan output`, `run apply`, `run cancel`,
`run create`, `run discard`, `run show`, `upload` and `workspace output list` — there
is no `tfci variable` and no `tfci variable-set`, and `internal/command/` holds no such
file. This section previously said variable management was "the one extension beyond
the CLI surface" and attributed all fourteen rows below to those commands; that was
never true of any released version we can find.

So the rule — anything `terraform`, `tofu` or `tfci` calls stays at `/api/tfe/v2/` —
does **not** by itself justify the table below. The routes are served and harmless, and
their real consumer is Terrapod's own web UI (`routers/variables.py` names the three
pages). **Whether they still belong on the frozen surface is an open question, tracked
on #2368** — nothing has moved, and nothing should move without that decision.

The one thing `tfci` genuinely needs that the CLI does not is `workspace output list`,
which reads `GET /workspaces/{id}/current-state-version-outputs` — served since #2376.

| Method | Path | go-tfe method | Caller |
|---|---|---|---|
| POST | `/api/tfe/v2/workspaces/{id}/vars` | `Variables.Create` | web UI (no `tfci` command exists) |
| PATCH | `/api/tfe/v2/workspaces/{id}/vars/{id}` | `Variables.Update` | web UI (no `tfci` command exists) |
| DELETE | `/api/tfe/v2/workspaces/{id}/vars/{id}` | `Variables.Delete` | web UI (no `tfci` command exists) |
| GET | `/api/tfe/v2/organizations/default/varsets` | `VariableSets.List` | web UI (no `tfci` command exists) |
| POST | `/api/tfe/v2/organizations/default/varsets` | `VariableSets.Create` | web UI (no `tfci` command exists) |
| GET | `/api/tfe/v2/varsets/{id}` | `VariableSets.Read` | web UI (no `tfci` command exists) |
| PATCH | `/api/tfe/v2/varsets/{id}` | `VariableSets.Update` | web UI (no `tfci` command exists) |
| DELETE | `/api/tfe/v2/varsets/{id}` | `VariableSets.Delete` | web UI (no `tfci` command exists) |
| GET | `/api/tfe/v2/varsets/{id}/relationships/vars` | `VariableSetVariables.List` | web UI (no `tfci` command exists) |
| POST | `/api/tfe/v2/varsets/{id}/relationships/vars` | `VariableSetVariables.Create` | web UI (no `tfci` command exists) |
| PATCH | `/api/tfe/v2/varsets/{id}/relationships/vars/{id}` | `VariableSetVariables.Update` | web UI (no `tfci` command exists) |
| DELETE | `/api/tfe/v2/varsets/{id}/relationships/vars/{id}` | `VariableSetVariables.Delete` | web UI (no `tfci` command exists) |
| POST | `/api/tfe/v2/varsets/{id}/relationships/workspaces` | `VariableSets.ApplyToWorkspaces` | web UI (no `tfci` command exists) |
| DELETE | `/api/tfe/v2/varsets/{id}/relationships/workspaces` | `VariableSets.RemoveFromWorkspaces` | web UI (no `tfci` command exists) |

**Verification:** When updating this section, check the `tfci` source at https://github.com/hashicorp/tfc-workflows-tooling — the `internal/cloud/` package exposes the call sites.

**Out of scope for tfci compat:** teams, projects, run tasks (TFE-shape), notifications, OAuth client management, the `hashicorp/tfe` Terraform provider's full surface. Those have structural divergence in Terrapod (single-org, label-RBAC instead of teams) that compatibility cannot bridge.

## Module Registry (CLI Download Protocol)

Service-discovered via `modules.v1` URL.

| Method | Path |
|---|---|
| GET | `/api/tfe/v2/registry/modules/{namespace}/{name}/{provider}/versions` |
| GET | `/api/tfe/v2/registry/modules/{namespace}/{name}/{provider}/{version}/download` |

The download endpoint returns 204 with `X-Terraform-Get` header pointing at the actual tarball URL.

## Provider Registry (CLI Download Protocol)

Service-discovered via `providers.v1` URL.

| Method | Path |
|---|---|
| GET | `/api/tfe/v2/registry/providers/{namespace}/{name}/versions` |
| GET | `/api/tfe/v2/registry/providers/{namespace}/{name}/{version}/download/{os}/{arch}` |

The platform-download endpoint returns JSON with `download_url`, `shasums_url`, and `shasums_signature_url` — the CLI then follows those URLs (which may be absolute, presigned, or server-relative).

## Maintaining This Document

When OpenTofu adds new go-tfe call sites in `internal/cloud/*` or `internal/backend/remote/*`, this list grows. Re-verify by:
1. `git clone https://github.com/opentofu/opentofu`
2. `grep -RP 'tfe\.[A-Z]\w+\(' internal/cloud internal/backend/remote --include='*.go' | grep -v _test.go`
3. For each match, find the go-tfe method's `client.NewRequest(...)` line in https://github.com/hashicorp/go-tfe to extract the path
4. Add to the relevant table above
5. For `POST /runs`, diff `RunCreateOptions` in go-tfe's `run.go` against the
   run-create attribute table above. A new field there is a new thing the CLI
   may send, and step 2 will not find it — the call site does not change when
   HashiCorp adds an option to a struct it already calls.
6. Grep `internal/cloud` and `internal/backend/remote` for literal URL path
   fragments, not just `tfe.` call sites. Step 2 structurally cannot find a route
   the CLI reaches with a hand-rolled request: `GET /plans/{id}/json-output-redacted`
   is built inline with `retryablehttp` and is not a `go-tfe` method at all, which
   is why it was absent from this document for as long as it existed.
7. Check both directions of the closure — that every route catalogued here is
   actually served, and that every `/api/tfe/v2` route the app serves is
   catalogued. Steps 1-6 only ever add rows; they cannot tell you a row has gone
   stale. Three of the rows above were found unserved that way.

Endpoints **not** verified by this process belong at `/api/v1/`, regardless of go-tfe lineage.
