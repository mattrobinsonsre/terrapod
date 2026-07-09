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

Policy evaluation runs **on the runner**, between the plan phase and
posting plan-result. The runner already has the plan JSON locally (it
just produced it with `tofu show -json`), so there's no JSON download,
no JSON-wait timing, and no concurrent-eval CPU load on the API:

1. The runner finishes the plan and runs `tofu show -json tfplan`.
2. The runner fetches the applicable policy bundle from the API
   (`GET /api/terrapod/v1/runs/{id}/policy-bundle`). The API answers
   that one question — which sets apply to this workspace — using the
   label-scope model above. An empty bundle means no policy sets in
   scope; the runner skips evaluation entirely.
3. For each applicable set, the runner runs `opa eval` once per policy
   against the local plan JSON, building a per-policy result with
   violation messages.
4. The runner POSTs all results to
   `POST /api/terrapod/v1/runs/{id}/policy-results`, **before** posting
   plan-result. One `policy_evaluation` row is recorded per set.
5. The runner posts plan-result. The API's post-plan gate is now just
   a database query — "is there a mandatory unoverridden failure for
   this run?":
   - **No** → the run advances to `planned` / `confirmed` / apply.
   - **Yes** → the run is held in `planning` (it is **not** errored).
     The block is surfaced on the run's **Policy Checks** panel.
6. **Advisory** set failures are recorded and shown but never block.

Speculative (plan-only) runs are evaluated and recorded but never
gated — there is no apply to block.

If the runner can't produce the plan JSON or `opa eval` itself fails
on a policy, the runner records an `errored` outcome for that set
(fail-closed for mandatory sets). If the runner can't fetch the
bundle at all after bounded retries, the run fails — never silently
skipping the gate.

## Overriding a blocked run

A workspace **admin** can override a run blocked by a mandatory policy
failure from the run's Policy Checks panel ("Override & Continue"). The
override is recorded against each failed evaluation (`overridden_by`),
and the run is released to continue immediately. Alternatively, the run
can be cancelled — a policy-blocked run sits in `planning`, so `cancel`
(not `discard`, which is for `planned` runs awaiting confirmation) is
the right action.

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

- The `opa` binary is **bundled in both the runner and the API images**
  at a pinned version (currently OPA 1.18.0). The runner is where
  evaluation actually happens — it has the plan JSON locally and scales
  out with K8s. The API keeps OPA only for `opa check` (write-time Rego
  validation, so broken syntax is rejected at policy save time rather
  than at the next run). The operator controls the OPA version by
  choosing the Terrapod image tag; there is no per-policy-set version
  selection.
- Policy enforcement is **opt-in**: with no policy sets defined, runs
  behave exactly as before.
- See the [runbook](runbooks.md#policy-enforcement-blocking-all-runs)
  for recovering from a policy set that is unintentionally blocking
  runs fleet-wide.
