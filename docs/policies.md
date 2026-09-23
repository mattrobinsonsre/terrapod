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

**Policy sets are evaluated for Pulumi runs too**, against a different input —
see [What a policy can read](#what-a-policy-can-read). A set scoped to a Pulumi
workspace is enforced exactly as it is on a Terraform one: the runner evaluates
applicable sets before reporting the preview, and a mandatory failure holds the
run. The two inputs are not interchangeable, so a rule written against
Terraform's plan JSON will not match a Pulumi resource, and vice versa.

**Security scanning is still not available for Pulumi runs.** Checkov and Trivy
read Terraform plan JSON. A Pulumi workspace cannot have an enforced scan
turned on, so its applies are never held waiting for a result that cannot
arrive; the run's scan endpoint says why in `meta.not-evaluated-reason`.

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

`data.terrapod_context` is the same whatever the engine. `input` is **the
engine's own account of the change**, so it differs between them.

| Reference | Contents |
|---|---|
| `input` (Terraform) | The raw `terraform show -json` plan document — `input.resource_changes`, `input.planned_values`, etc. Existing community Terraform Rego works unchanged. |
| `input` (Pulumi) | A document built from the preview's engine event log — see below. |
| `data.terrapod_context` | Terrapod metadata: `workspace` (`id`, `name`, `labels`) and `run` (`id`, `message`, `source`, `is_destroy`, `plan_only`). |

#### The Pulumi input

`pulumi preview` has no equivalent of `terraform show -json`, so Terrapod builds
one from the engine event log the preview writes:

Note the counts and the array: nine resources are unchanged and do not appear,
because `resource_changes` carries only what the preview will touch.

```json
{
  "engine": "pulumi",
  "change_summary": {"create": 1, "same": 9},
  "has_changes": true,
  "resource_changes": [
    {
      "op": "create",
      "urn": "urn:pulumi:dev::shop::aws:s3/bucket:Bucket::assets",
      "type": "aws:s3/bucket:Bucket",
      "name": "assets",
      "parent": "urn:pulumi:dev::shop::pulumi:pulumi:Stack::shop-dev",
      "provider": "urn:pulumi:dev::shop::pulumi:providers:aws::default::uuid",
      "custom": true,
      "protect": false,
      "inputs": {"acl": "public-read"},
      "diffs": ["acl"],
      "detailed_diff": {"acl": {"kind": "update"}}
    }
  ]
}
```

`resource_changes` deliberately echoes Terraform's key, so the shape of a rule
carries across even though the contents do not:

```rego
package terrapod

# No public buckets, whichever engine declares them.
deny contains msg if {
    some rc in input.resource_changes
    rc.type == "aws:s3/bucket:Bucket"
    rc.inputs.acl == "public-read"
    msg := sprintf("%s is public-read", [rc.name])
}
```

Four things to know before writing one:

- **Only changing resources appear.** `resource_changes` means changes, as it
  does on Terraform: a resource the preview reports as `same` is not carried,
  so the rule above cannot deny a bucket that is not being touched. There is no
  Pulumi equivalent of Terraform's `planned_values`, so a policy cannot inspect
  the unchanged remainder of a stack — `change_summary.same` counts it, and
  that is all. Guarding on `rc.op` is still good practice when a rule should
  only fire for, say, a `create`.
- **`inputs` is what the program declared**, the analogue of Terraform's
  `change.after`. The resource's `outputs` are not carried: they are the
  provider's complete returned state, which adds little to a decision and a
  great deal to the document. **On a `delete` there is no new state**, so the
  entry describes the resource as it exists today — which is what a rule like
  "do not delete a protected resource" needs, and why `protect` is meaningful
  on a deletion rather than always `false`.
- **A marked secret is invisible to policy, and this is not parity with
  Terraform.** The engine replaces any property marked secret with the literal
  string `"[secret]"` before writing the log, so
  `deny if rc.inputs.encrypted == false` silently stops firing the moment a
  program author wraps that value in `pulumi.secret()` — the property becomes a
  truthy string. Terraform's plan JSON carries sensitive values in the clear and
  flags them in `after_sensitive`, so the equivalent Terraform rule still works.
  **A mandatory Pulumi gate can therefore be evaded from inside the program it
  governs.** Write rules against properties a program has no reason to mark
  secret, and treat `"[secret]"` as a value worth denying on where it matters.
- **A preview that did not finish is not evaluated.** Without the engine's
  summary event there is no honest account of the change, so no policy runs
  rather than one deciding on a partial list.

Separately, a value that is sensitive but was never marked secret arrives in
the clear — the same gap Terraform's plan JSON has. Do not treat this input as
scrubbed.

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
   (`GET /api/v1/runs/{id}/policy-bundle`). The API answers
   that one question — which sets apply to this workspace — using the
   label-scope model above. An empty bundle means no policy sets in
   scope; the runner skips evaluation entirely.
3. For each applicable set, the runner runs `opa eval` once per policy
   against the local plan JSON, building a per-policy result with
   violation messages.
4. The runner POSTs all results to
   `POST /api/v1/runs/{id}/policy-results`, **before** posting
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
and the run is released to continue immediately. Alternatively, discard
the run, or queue a newer one, which supersedes it.

The `tofu`/`terraform` CLI can show a failed policy and override it too, when the
deployment reports runs in the Terraform Enterprise vocabulary — see
[post-plan-decisions.md](post-plan-decisions.md).

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

- The `opa` binary is **not bundled in the images**. It is pulled through
  the binary cache on demand, and its version is an ordinary Helm value:

  ```yaml
  api:
    config:
      registry:
        platform_tools:
          opa_version: "1.19.0"
  ```

  One version for the whole deployment — there is no per-policy-set
  selection. The upside over baking it in is that an upstream OPA fix
  reaches you with a `helm upgrade` instead of waiting for a Terrapod
  release. The runner fetches it **only when a policy set actually
  applies to the run**, so a deployment with no policies never downloads
  it.
- **The runner's policy path fails closed.** If OPA cannot be obtained
  and policy sets apply, the run errors rather than proceeding. A policy
  that cannot be evaluated must not pass a gate it was meant to block.
- The API also uses OPA, for `opa check` only — write-time Rego syntax
  validation, so broken syntax is rejected at policy save time rather
  than at the next run. **That path degrades rather than failing:** if
  OPA is unavailable, the save is accepted with a warning that validation
  was skipped. Rego that slips past a *syntax* check is still evaluated
  on the runner, which fails closed — whereas refusing the write would
  leave you unable to edit any policy because of an unrelated fetch
  failure.
- **Air-gapped / sealed installs** must pre-warm OPA like any other
  cached artifact. The bulk-warm endpoint and the post-install warm Job
  include it automatically — they derive the entry from the configured
  version, so there is nothing to add to a warm manifest.
- Policy enforcement is **opt-in**: with no policy sets defined, runs
  behave exactly as before.
- See the [runbook](runbooks.md#policy-enforcement-blocking-all-runs)
  for recovering from a policy set that is unintentionally blocking
  runs fleet-wide.
