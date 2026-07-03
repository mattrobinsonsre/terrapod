# Execution Hooks

Execution hooks let an operator run **custom shell steps inside the runner Job**
at fixed points around `terraform`/`tofu` execution — a lighter answer than
building a custom runner image when an environment needs a one-off step (drop an
`/etc/hosts` entry, authenticate to a secret backend before `init`, install an
extra CLI, run a pre-flight check, notify an external system after apply).

A hook is a **reusable library entry**, administered centrally and **associated
with the workspaces that use it** — the same model as variable sets. There is
**no global scope**: a hook runs only on workspaces it is explicitly associated
with, so no single hook can reach every workspace's secrets or state.

> Execution hooks supersede the older, never-fully-wired `setup_script` /
> `TP_SETUP_SCRIPT` runner slot — the `pre_init` hook point is its drop-in
> replacement.

## Hook points

Each hook fires at one of five points, inside the runner Job, with the run's
environment, working directory, and cloud identity already established:

| Point | Runs | Typical use |
|---|---|---|
| `pre_init` | before `init` | cloud/secret auth, `/etc/hosts`, extra tooling, cert fetch |
| `pre_plan` | after `init`, before `plan` | pre-plan validation |
| `post_plan` | after a successful `plan` | export/inspect the plan, external gate |
| `pre_apply` | after confirm, before `apply` | last-mile checks (apply phase only) |
| `post_apply` | after a successful `apply` + state upload | notify, register, cleanup |

When several hooks share a point on a workspace, they run in `(priority, name)`
order — lower `priority` first, ties broken by name.

## Failure behaviour

**Any hook exiting non-zero fails the run**, surfaced with a clear error. For
`post_apply` the apply has already succeeded and state is already saved, so the
run is marked errored with an "apply succeeded; post-apply hook failed" message
— the broken hook is visible, and your state is never lost.

## Security

A hook is operator-supplied shell that runs with the **runner's cloud identity**
and can read the run's environment (including resolved variables). The trust
boundary is the same as "who can run `terraform` here", so:

- Managing hooks and their associations requires platform **`admin`**.
- Every hook create/edit/associate is **audit-logged**.
- Secrets a hook needs should come from **workspace variables**, delivered via
  the per-run Secret — never inlined into the hook body (the repo is
  world-readable via the API/UI/provider).
- A deployment can **forbid hooks entirely** with the platform kill-switch (see
  below).

## Enabling / kill-switch

Execution hooks are available by default. The platform kill-switch is the Helm
value **`runners.hooksEnabled`** (default `true`); set it to `false` to stop the
listener from ever delivering hooks to runner Jobs — for sealed or
security-conscious deployments that want to disallow custom-shell hooks:

```yaml
runners:
  hooksEnabled: false
```

## Managing hooks

Hooks are managed three ways, all over the same API:

- **Web UI** — *Admin → Execution Hooks* (list/create/edit/delete) and the
  **Workspaces** tab on a hook to associate/dissociate workspaces.
- **Terraform provider** — `terrapod_execution_hook` +
  `terrapod_execution_hook_workspace`:

  ```hcl
  resource "terrapod_execution_hook" "hosts_entry" {
    name        = "internal-hosts-entry"
    hook_point  = "pre_init"
    script      = "echo '10.0.0.5 registry.internal' >> /etc/hosts"
  }

  resource "terrapod_execution_hook_workspace" "prod" {
    hook_id      = terrapod_execution_hook.hosts_entry.id
    workspace_id = terrapod_workspace.prod.id
  }
  ```

- **API** — `POST /api/terrapod/v1/execution-hooks` and
  `.../relationships/workspaces`. See the
  [API Reference](api-reference.md#execution-hooks).

Building a custom runner image remains the right choice for *baking in tools*
your runs always need; execution hooks are for *per-environment steps*.
