# Terragrunt

Terrapod supports [Terragrunt](https://terragrunt.gruntwork.io/) as an execution
wrapper around `tofu`/`terraform`. There are two independent paths, and they have
very different requirements.

## TL;DR

| Path | How | Terrapod config needed |
|---|---|---|
| **CLI-driven** (local execution) | Run `terragrunt plan`/`apply` yourself; the unit's backend points at Terrapod's `cloud` block | **None** — works out of the box |
| **Agent mode** (server-side runs) | Set `terragrunt_enabled = true` on the workspace; Terrapod's runner invokes terragrunt | A workspace flag + version |

## CLI-driven runs

If you already run Terragrunt locally or in CI, nothing special is required.
Configure the unit's backend to use Terrapod's cloud backend and run terragrunt
as usual:

```hcl
# terragrunt.hcl
remote_state {
  backend = "local"   # or generate a cloud block — either works
}

generate "backend" {
  path      = "backend.tf"
  if_exists = "overwrite"
  contents  = <<EOF
terraform {
  cloud {
    hostname     = "terrapod.example.com"
    organization = "default"
    workspaces { name = "my-workspace" }
  }
}
EOF
}
```

```console
$ terragrunt plan
$ terragrunt apply
```

Terragrunt drives `tofu`, `tofu` talks to Terrapod's state backend, and Terrapod
records state versions, locking, and run history exactly as it does for a bare
`tofu` cloud-backend run. Terrapod isn't involved in *how* terragrunt resolves
the binary or inputs — it only sees the resulting state operations.

## Agent mode (server-side runs)

Agent mode runs terragrunt **on the Terrapod runner**, so Terrapod fetches the
terragrunt binary, injects the workspace variables, and captures state itself.

Enable it per workspace:

- **UI** — Workspace → Overview → Edit → **Terragrunt** → enable + set a version.
- **API** — `terragrunt-enabled` / `terragrunt-version` attributes on the
  workspace (see [api-reference.md](api-reference.md)).
- **Provider** — `terragrunt_enabled` / `terragrunt_version` on
  `terrapod_workspace`.

```hcl
resource "terrapod_workspace" "tg" {
  name              = "my-terragrunt-workspace"
  execution_mode    = "agent"
  agent_pool_id     = terrapod_agent_pool.default.id
  terragrunt_enabled = true
  terragrunt_version = "1.0"   # partial versions like "1.0" are resolved by the cache
}
```

### How it works

1. **Binary cache.** The runner fetches the terragrunt binary from Terrapod's
   pull-through binary cache (`/api/terrapod/v1/binary-cache/terragrunt/{version}/{os}/{arch}`),
   the same mechanism used for `tofu`/`terraform`. Terragrunt ships a bare
   per-platform binary, which the cache serves directly. Partial versions
   (`1.0`) resolve to the latest matching release.
2. **Binary wrapping.** The runner points terragrunt at the cached `tofu`/`terraform`
   binary via the `TG_TF_PATH` environment variable, so the workspace's
   configured execution backend + version are exactly what terragrunt runs.
3. **Backend reconciliation.** Before each `tofu` invocation Terrapod injects a
   local-backend override, so whatever `remote_state` / `generate` blocks your
   Terragrunt config produces, **Terrapod still owns state** — it is captured
   from the runner and stored as a normal state version. You do not configure a
   `cloud` block for agent-mode terragrunt.
4. **Working directory.** Terragrunt copies each unit into a
   `.terragrunt-cache/…` directory and runs `tofu` there; Terrapod follows it
   into that directory for state download/upload and the plan file. Your
   workspace's existing state is made available to that run, so `inputs`,
   refreshes, and incremental plans behave normally.

### Variables

Workspace variables resolve server-side exactly as for non-terragrunt agent runs
and are delivered via the per-run vars Secret: terraform-category vars as a
generated `terrapod.auto.tfvars` file (mounted from the Secret) and env-category
vars via `secretKeyRef` — never plaintext in the Job spec. The generated
`terrapod.auto.tfvars` is copied into Terragrunt's `.terragrunt-cache` unit and
read by the underlying tofu/terraform. Terragrunt's own `inputs = { … }` block is
independent and still honoured (it becomes `TF_VAR_*` for the run). Precedence
between the two follows Terraform's normal variable precedence.

### Limitations (current)

- **Single unit per workspace.** A workspace runs one Terragrunt unit (the
  working directory). `terragrunt run-all` / multi-unit stacks are not executed
  as a single agent run — model each unit as its own Terrapod workspace and use
  [run triggers](api-reference.md) for cross-unit ordering.
- **`terraform { source = … }` remote modules** are fetched by terragrunt at run
  time; the runner needs network access to that source (as it does for any
  module download).
- Agent mode is the supported server-side path. For complex multi-unit
  orchestration today, the CLI-driven path (above) remains available with zero
  Terrapod-side configuration.

### Default version

If `terragrunt_enabled` is true and no version is set, Terrapod uses `1.0` (the
latest stable Terragrunt line at time of writing). Pin an exact `x.y.z` for
reproducibility.
