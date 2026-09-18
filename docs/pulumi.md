# Pulumi workspaces and runs

Terrapod is a Terraform and OpenTofu orchestrator that also runs Pulumi. A
Pulumi workspace is coerced into the same flow as every other workspace — one
workspace per stack, a run with a phase you review and a phase that executes
what you reviewed, under the same RBAC, notifications, run triggers and audit
trail. Where Pulumi genuinely differs, this page says so rather than implying
parity.

Pulumi is off until an operator turns it on:

```yaml
api:
  config:
    engines:
      pulumi:
        enabled: true
```

With it off, nothing Pulumi-related is registered, and Terraform and OpenTofu
are untouched either way.

## A workspace is a stack

A Pulumi workspace is named `{project}::{stack}`, which is how Terrapod
recovers Pulumi's `{org}/{project}/{stack}` without growing a "project" concept
of its own. The organization is always `default`, as everywhere else in
Terrapod.

## What a run does

| Phase | Terraform | Pulumi |
|---|---|---|
| The phase you review | `terraform plan` | `pulumi preview` |
| The phase that executes it | `terraform apply` | `pulumi up` |

The run's status names are the platform's and do not change — a run is
`planning` whatever engine it belongs to — but the words shown to a person are
the engine's, so a Pulumi run previews and updates.

### Run options

Every option means on a Pulumi run what it means on a Terraform run (#1559).
The phase you review always previews **the operation that will run**, never a
different one:

| Option | Pulumi |
|---|---|
| Destroy | `pulumi destroy --preview-only`, then `pulumi destroy --yes` |
| Refresh-only | `pulumi refresh --preview-only`, then `pulumi refresh --yes` |
| Target addresses | `--target <urn>`, once per resource |
| Replace addresses | `--replace <urn>` |
| Refresh | `--refresh=true` / `--refresh=false`, always explicit |
| Parallelism | `--parallel` |

A Pulumi resource is addressed by its URN, so that is what a targeted or
replaced Pulumi run takes; the API attribute is `target-addrs` either way.

### Execution hooks

A workspace's `pre_plan`, `post_plan`, `pre_apply` and `post_apply` hooks run
for a Pulumi run at the same four points, around the preview and the update. A
hook that exits non-zero fails the run, so a `pre_apply` hook that refuses means
nothing is applied.

### Which Pulumi version a run uses

The workspace pins it, in the same `engine-version` attribute a Terraform
workspace uses for its own version (#1559) — so two Pulumi workspaces can sit on
different CLI versions, and upgrading one does not move the others.

| | |
|---|---|
| **Partial versions** | `3.208` means the newest `3.208.*`. An exact `3.208.2` is taken as written. |
| **Unset** | The deployment's `default_pulumi_version`. |
| **Where it comes from** | The same pull-through binary cache that serves `tofu` and `terraform`, so a runner needs no reach to Pulumi's releases. |
| **Verification** | The artifact's SHA-256 against the checksum Pulumi publishes for that release, fail-closed. There is no signature to check — Pulumi signs nothing — so `binary_cache.verify: signature` means "the strongest available", which here is that checksum. |

Pulumi publishes no static version index, only a plain-text "latest version"
endpoint, so resolving a partial reads the GitHub releases API and inherits its
rate limit. Point `binary_cache.pulumi_version_index_url` at a mirror of the same
shape if that bites. A **sealed** deployment resolves only against what is
already cached: the default version is warmed for you, but a workspace pinned to
anything else must be listed in the warm manifest, exactly as a Terraform
workspace on a non-default version must be.

With the Pulumi engine switched off, none of this is reachable — asking the
cache to list Pulumi versions is refused rather than answered, so a
Terraform-only deployment makes no requests on Pulumi's behalf and warms no
Pulumi binary.

### What language a program can be written in

A Pulumi program is written in a real language, and the runner has to be able to
run it. That means two things Terraform never needs: the language's own
toolchain, and the program's dependencies.

| Runtime | Status |
|---|---|
| `yaml` | Works. The CLI interprets it; there is no toolchain and nothing to install. |
| `nodejs` (TypeScript and JavaScript) | Works. Node is fetched through the binary cache and `npm ci` (or `npm install`) runs against Terrapod's npm proxy before the preview. |
| `python`, `go`, `dotnet` | Refused, by name, with a message saying so. Tracked on #1566. |

The refusal is deliberate: a program Terrapod cannot run fails at the start with
a sentence naming its runtime, rather than part-way through Pulumi with an error
about a missing language host.

**Node is not baked into the runner image.** It is pulled through the same cache
that serves `pulumi`, `tofu` and `terraform`, so a Terraform-only deployment
carries none of it and a sealed one serves it from its own cache. The version is
`default_node_version` in your values — partial, like the others, so `22` means
the newest 22.x. A program's own `engines.node` range is not honoured: the
runtime is a property of the platform, not of the repository.

**Dependencies are installed in both phases.** The preview and the update run in
different pods, so `node_modules/` cannot carry over from one to the other. The
install therefore happens twice, inside each Job's own timeout — and `npm ci` is
used whenever there is a `package-lock.json`, so both phases resolve to exactly
the same tree.

The npm credential is written to a `.npmrc`, never passed on a command line: the
runner streams its logs to the API and the UI.

### Binding an update to its preview

`pulumi-bind-plan` on the workspace makes the update perform exactly the
operations the preview showed, which is what `plan -out` / `apply <file>` gives
a Terraform run. It is **off by default**, because Pulumi's update plans are
still experimental upstream. Unbound, the update works out its own changes from
the same configuration — the same degradation Terrapod accepts when a Terraform
plan artifact is unavailable. The run reports which it is, in
`pulumi-bind-plan`, so it is visible at the moment of approval. A destroy or
refresh-only run is never bound: neither command takes a plan.

### What the preview reports

The preview writes its engine events, which Terrapod reduces to a digest
uploaded as the run's plan artifact (#1560): the change summary, and each step's
operation, URN and type. That is what fills the change badges, decides a
zero-change run, and lets conditional auto-apply judge a preview. The digest
deliberately carries no resource state — Pulumi's events include every
resource's old and new values, which is where a stack's secrets are.

## Where Pulumi is not coerced, and why

- **State never lives in Terrapod's Pulumi service during an agent run.** The
  stack is imported into the Job, worked on against a file backend, and handed
  back once at the end — exactly as a Terraform run downloads and uploads its
  state. The service surface (`pulumi login`) is for local-mode use.
- **OPA policy sets and security scanning do not apply yet.** Both read
  Terraform plan JSON, which a Pulumi preview does not produce. Rather than
  holding every apply for an evaluation that cannot happen, policy sets are not
  evaluated for Pulumi runs and scanning is refused on a Pulumi workspace
  (#1567). The run says so in `meta.not-evaluated-reason`.

## See also

- [`docs/pulumi-cli-surface.md`](pulumi-cli-surface.md) — the slice of Pulumi's
  service protocol Terrapod implements, for `pulumi login` against it.
- [`docs/policies.md`](policies.md) and
  [`docs/security-scanning.md`](security-scanning.md) — the gates, and what they
  currently do on a Pulumi workspace.
