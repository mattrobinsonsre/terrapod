# Post-plan decisions

Three gates can stop a run after its plan has finished and before it applies:

| Gate | Holds the run when | Released by |
|---|---|---|
| [Run tasks](run-tasks.md) | a `mandatory` post-plan task fails | overriding the task stage |
| [Policy sets](policies.md) | a `mandatory` policy set fails or errors | overriding the policy checks |
| [Security scanning](security-scanning.md) | an `enforced` scan fails or errors | overriding the scan |

While a gate holds a run, the run is waiting for someone to decide: override
the gate, discard the run, or queue a newer run, which supersedes it. It is not
stuck, and it does not time out: the plan Job that produced it can be cleaned
up long before anyone decides. This page is about how Terrapod reports such a
run, and how the `tofu`/`terraform` CLI handles one.

## Two ways of reporting a held run

Terrapod reports a held run in one of two vocabularies. A deployment picks the
default; a client can ask for either one.

### The 1.x vocabulary (the default until 2.0)

The run reports `status: planning`, the same status as a plan still running.
Two things tell them apart:

- `blocked-by` names the gate holding the run: `run-task`, `policy` or
  `security-scan`. It is `null` for any run that is not held.
- The run's plan reports `status: finished`.

A held apply run is discardable (`actions.is-discardable: true`). A failed
mandatory run task errors the run instead of holding it.

The CLI does not understand this: `tofu apply` prints the plan, sees a run it
cannot confirm, and exits 0 without applying and without saying why. Open the
run in the UI to see what holds it.

### The Terraform Enterprise vocabulary (the default from 2.0)

The run reports the status Terraform Enterprise uses for the same situation:

| Status | Meaning |
|---|---|
| `post_plan_running` | Post-plan run tasks are still running |
| `post_plan_awaiting_decision` | A mandatory run task failed |
| `policy_override` | A mandatory policy set or an enforced security scan failed |

`blocked-by` is still reported, unchanged. The run also lists its **policy
checks** and **task stages** in its relationships, which is what the CLI reads.
A failed mandatory run task holds the run, as a policy failure does, instead of
erroring it. A plan-only run still errors, since it has nothing to apply.

These statuses are only ever reported. The run is stored as `planning`, so
switching vocabulary rewrites nothing and can be undone at any time.

## Choosing the vocabulary

The deployment-wide default is a Helm value:

```yaml
api:
  config:
    runs:
      tfe_post_plan_decisions: false   # true for the Terraform Enterprise vocabulary
```

It defaults to `false` in 1.x and flips to `true` in 2.0.0; see
[upgrading-to-2.0.md](upgrading-to-2.0.md). The setting also decides whether a
failed mandatory run task holds or errors a run.

A client can override the default for a single request with a header:

```
X-Terrapod-Post-Plan-Decisions: tfe       # or: legacy
```

The header changes only what that response reports. It is how a script or
integration moves to the new statuses before the operator flips the default, or
stays on the old ones for a while after. The CLI cannot send it, so the CLI
follows the default.

## What the CLI does

With the Terraform Enterprise vocabulary, `tofu apply` (or `terraform apply`)
handles a held run the way it would on Terraform Enterprise:

1. It prints the post-plan task results, then each policy check's output: the
   denying policies and their messages, or the scan's findings, worst first.
2. On a failed check it asks:

   ```
   Do you want to override the soft failed policy check?
     Only 'override' will be accepted to override.
   ```

   Answering `override` overrides the check, and the run moves on at once to
   the usual apply confirmation. Any other answer leaves the run held, to be
   overridden or discarded later.
3. With `-auto-approve` it overrides a failed policy check itself, **if** the
   caller is allowed to (admin on the workspace), and then applies. A caller
   who is not allowed to override gets an error naming the run instead.

A failed run task is offered for override the same way (`Do you want to
override the failed policy check?`); `-auto-approve` does not override a task
stage, matching Terraform Enterprise.

`tofu plan` on a speculative run that a policy fails exits with an error naming
the failed check.

Terrapod has two policy checks per run, each present only when its gate ran:
one for its OPA policy sets (shown as *Organization Policy Check*) and one for
its security scan (*Workspace Policy Check*). Overriding the OPA check
overrides every failed policy set on the run, as the Policy Checks panel does.

## API

The policy checks are on the Terraform Enterprise surface, with or without the
vocabulary switch:

```
GET  /api/tfe/v2/runs/{run_id}/policy-checks
GET  /api/tfe/v2/policy-checks/{id}
GET  /api/tfe/v2/policy-checks/{id}/output
POST /api/tfe/v2/policy-checks/{id}/actions/override
```

See [api-reference.md](api-reference.md#policy-checks) for the shapes, and
[tfe-cli-surface.md](tfe-cli-surface.md) for what the CLI calls. The
`terrapod_run_policy_checks` and `terrapod_policy_check_override`
[MCP tools](mcp.md) cover the same ground for an agent.
