# Policy-as-Code (OPA)

Terrapod enforces **policy-as-code** on runs using [Open Policy Agent
(OPA)](https://www.openpolicyagent.org/) and the Rego language. Policies
are evaluated against a run's plan after planning completes; a failing
**mandatory** policy blocks the apply, while an **advisory** policy only
records a warning.

This is the open-source equivalent of Terraform Enterprise's Sentinel
policy sets. Sentinel itself is proprietary and out of scope — OPA is
open source and is the supported engine.

## Concepts

| Concept | Description |
|---|---|
| **Policy set** | A named, admin-managed collection of policies with a single enforcement level and a workspace scope. |
| **Policy** | One Rego document inside a set. Must declare `package terrapod`. |
| **Enforcement level** | `advisory` (record a warning, never block) or `mandatory` (block the apply on failure). Set per policy set. |
| **Scope** | Which workspaces a set applies to — either `global` (every workspace) or label-based allow/deny rules. |
| **Policy evaluation** | The recorded outcome of one policy set against one run. |

There are no organizations, teams, or projects — policy sets are scoped
with the same label-based allow/deny model as roles.

## Scoping policy sets to workspaces

A policy set applies to a workspace when:

- the set is **enabled**, and
- `global_scope` is true — it applies to *every* workspace; or
- the workspace matches the set's **allow** rules (a label match or a
  name match) **and** does not match its **deny** rules.

Deny always wins over allow. Allow/deny labels are matched key-by-key:
the workspace matches if, for any rule key, the workspace's value for
that key is among the rule's accepted values. This is the same model
roles use, so "policy set for production" is just a set scoped to
`env: prod`.

## Writing a policy

A Terrapod policy is a Rego v1 document. It **must**:

1. declare `package terrapod`, and
2. express violations through a `deny` set of message strings.

An optional `warn` set carries non-blocking advisories.

```rego
package terrapod

# Block unencrypted S3 buckets.
deny contains msg if {
    some rc in input.resource_changes
    rc.type == "aws_s3_bucket"
    rc.change.actions[_] == "create"
    not rc.change.after.server_side_encryption_configuration
    msg := sprintf("S3 bucket %s is created without encryption", [rc.address])
}
```

A policy set **passes** when every policy's `deny` set is empty.

### What a policy can read

| Reference | Contents |
|---|---|
| `input` | The raw `terraform show -json` plan document — `input.resource_changes`, `input.planned_values`, etc. Existing community Terraform Rego works unchanged. |
| `data.terrapod_context` | Terrapod metadata: `workspace` (`id`, `name`, `labels`) and `run` (`id`, `message`, `source`, `is_destroy`, `plan_only`). |

```rego
package terrapod

# Production workspaces may not run destroy plans.
deny contains msg if {
    data.terrapod_context.workspace.labels.env == "prod"
    data.terrapod_context.run.is_destroy
    msg := "destroy runs are not permitted on production workspaces"
}
```

Rego must be **v1** (OPA 1.x syntax — `if` / `contains` keywords).
Terrapod is a new project and does not support the legacy Rego v0
syntax. The Rego is validated with `opa check` when a policy is created
or updated, so a syntax error is rejected immediately rather than at run
time.

## How enforcement works

Policy evaluation happens at the **post-plan** boundary, after the
runner has produced the plan and uploaded its JSON form:

1. The run finishes planning.
2. Terrapod resolves which policy sets apply to the workspace.
3. Each applicable set is evaluated against the plan JSON. One
   `policy_evaluation` row is recorded per set.
4. **Mandatory** set failed (or errored) → the run is held: it stays in
   `planning` and will not advance to `planned`/apply. The block is
   surfaced on the run's **Policy Checks** panel.
5. **Advisory** set failed → the failure is recorded and shown, and the
   run proceeds normally.
6. All mandatory sets passed → the run advances as usual.

Speculative (plan-only) runs are evaluated and their results shown, but
they are never blocked — there is no apply to gate.

If the runner could not produce the plan JSON, a mandatory set
fails closed (the run is held) — Terrapod never applies a run it could
not check.

## Overriding a blocked run

A workspace **admin** can override a run blocked by a mandatory policy
failure from the run's Policy Checks panel ("Override & Continue"). The
override is recorded against each failed evaluation (`overridden_by`),
and the run is released to continue immediately. Alternatively, the run
can be discarded.

## Managing policy sets

Policy sets are managed by platform admins under **Policy Sets** in the
admin area, or via the API (see
[api-reference.md](api-reference.md#policy-sets)):

- Create a set, choosing its enforcement level and scope.
- Add policies — the Rego is validated on save.
- Edit scoping (global, or allow/deny labels and names).
- Disable a set to stop it being evaluated without deleting it.

Deleting a policy set removes its policies but **keeps** the historical
`policy_evaluation` records of past runs (their set reference is nulled,
the set name is retained for display).

## Operational notes

- The `opa` binary is **bundled in the API image** at a pinned version
  (currently OPA 1.16.2). The operator controls the OPA version by
  choosing the Terrapod image tag — there is no per-policy-set version
  selection. Policy evaluation is entirely server-side; runners are not
  involved.
- Policy enforcement is **opt-in**: with no policy sets defined, runs
  behave exactly as before.
- See the [runbook](runbooks.md#policy-enforcement-blocking-all-runs)
  for recovering from a policy set that is unintentionally blocking
  runs fleet-wide.
