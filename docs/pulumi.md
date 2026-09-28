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

A stack can be created by hand, or discovered. **Autodiscovery understands
Pulumi**: a rule with `engine: pulumi` watches a monorepo for
`Pulumi.<stack>.yaml` files and creates a workspace per stack, so one directory
holding `Pulumi.dev.yaml` and `Pulumi.prod.yaml` yields two — the project half
of the name coming from the directory, the stack half from the filename. See
[Workspace Autodiscovery](autodiscovery.md).

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

### Stack configuration

A Pulumi program reads its settings from Pulumi's stack config, and a workspace
supplies them with variables in the **`pulumi_config`** category — Pulumi's
equivalent of the `terraform` category. They are ordinary workspace variables,
set the same way as any other (see [Variables](api-reference.md#variables)), so
they are encrypted at rest, can be delivered from a variable set, and can take
their value from [OpenBao (or HashiCorp Vault)](vault.md) at run time.

There is no tfvars file to render into, because Pulumi has none: config is a
flat key/value namespace the program reads at will, and nothing declares it in
advance. So Terrapod delivers it the way Pulumi's own users do — `pulumi config
set` against the selected stack, run before anything reads it. That happens
after the stack is selected and before the `pre_plan` hook, so a hook that
inspects `pulumi config` sees what the run will actually use rather than only
what the repository committed. The preview and the update run in different pods,
so it happens once in each, exactly as dependency installation does.

This is the agent-mode path. A `pulumi up` from your own machine uses the config
in your own checkout; a workspace's variables are not delivered to it.

**Keys pass through verbatim, and nothing is prefixed.** A variable keyed
`region` is set as `region`, and one keyed `aws:region` as `aws:region`. The
runner already executes in the project directory, so the CLI namespaces an
unqualified key to the project named in `Pulumi.yaml` itself — `region` becomes
`myproject:region` unaided — while the explicit `namespace:key` form, which is
how provider config such as `aws:region` is written, survives untouched.
Terrapod transforms neither, because a transformation here is a thing that can
be wrong.

Set `structured` on the variable and the key is set with `--path`, so
`outer.inner` writes a nested value rather than a literal dotted key — the same
distinction `structured` already draws for a `terraform` variable.

A value never reaches a command line. Pulumi takes it on stdin, so the run log
carries the key and the flags only — the same mechanism-rather-than-redaction
guarantee [private module source auth](module-auth.md) holds, and for the same
reason: the runner streams its output to the API and the UI. The value arrives
byte-for-byte, including one that genuinely ends in a newline, which is what
keeps a PEM key intact.

#### A sensitive value becomes a real Pulumi secret

A `pulumi_config` variable marked `sensitive` is set with `--secret`. The value
is then encrypted by the stack's own secrets provider — in agent mode, the one
Terrapod's service backend holds — and Pulumi's *engine* renders it as
`[secret]` in the preview a reviewer reads, in the event log, and in any state it
reaches. Delivered as ordinary config it would sit in plaintext in the stack
config file and in the preview output, which is the wrong thing to arrive at by
omission.

Two things it does **not** buy:

- **The masking is Pulumi's, not Terrapod redacting the log.** It covers what
  the engine prints about the value. A program that reads the value and prints
  it itself has printed it.
- **It does not change how Terrapod holds the variable.** That is the ordinary
  sensitive-variable path — encrypted at rest, never returned by the API.

A stack whose secrets provider has been moved to a passphrase or cloud KMS
cannot run on an agent at all, secrets or not; see
[`docs/pulumi-cli-surface.md`](pulumi-cli-surface.md).

#### Merging with a committed `Pulumi.<stack>.yaml`

`pulumi config set` edits the stack's config file in place, so the merge is **per
key, and Terrapod's value wins**. Given this in the repository:

```yaml
# Pulumi.dev.yaml
config:
  myproject:replicas: "2"
  myproject:tier: standard
```

and one workspace variable keyed `replicas` with the value `5` — unqualified, so
the CLI namespaces it to `myproject:replicas` — the run sees:

| Key | Value | Where it came from |
|---|---|---|
| `myproject:replicas` | `5` | The workspace, overwriting the committed `2` |
| `myproject:tier` | `standard` | The repository. The workspace sets no such key, so the committed one survives untouched |

That is the right way round because a value held in Terrapod is rotatable,
RBAC'd and audited, and a committed one is none of those.

**A config failure fails the run.** If a key cannot be set, the run stops there
rather than dropping the entry with a warning. A program running without config
someone set deliberately is doing something nobody asked for: `config.get` with
a default would quietly take the default, and only `config.require` would
complain at all.

#### What Terrapod cannot tell you about config

**Pulumi reports nothing about config that was set and never read.** There is no
unused-config signal in the CLI or the engine, so Terrapod cannot tell you that
a key your program never looks at is doing nothing.

What it can tell you is narrower, and worth not confusing with the above:
whether a variable is in a category this workspace's engine reads **at all**.
Every workspace variable reports `applies-to-engine`, and a workspace holding
one its engine never consumes raises a `variables_not_consumed` health condition
(severity `warning`). The rule is symmetric — a `pulumi_config` variable on a
Terraform workspace and a `terraform` variable on a Pulumi one are equally inert
and equally flagged. Both are computed from the workspace's own variables: a
variable-set variable has no single owning workspace, so it carries no
`applies-to-engine` and does not raise the condition.

Writing one is never refused, and that is deliberate. Variables are data, and
which of them apply is decided at run time by the engine that runs, so a
category mismatch is **surfaced rather than rejected**: the failure worth naming
is not the write, it is a variable an operator sets, sees stored, and watches do
nothing.

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
| `python` | Works. A virtualenv is built and `requirements.txt` installed into it from Terrapod's PyPI proxy. |
| `go` | Works. The Go toolchain is fetched through the binary cache and `go mod download` runs against Terrapod's Go module proxy. |
| `dotnet` (C#, F#, VB) | Works. The .NET SDK is fetched through the binary cache and `dotnet restore` runs against Terrapod's NuGet proxy. |

A runtime Pulumi does not have — or a typo — is still refused by name, rather
than guessing at an install.

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

The npm credential is written to a `.npmrc` and pip's to a `.netrc`, never passed
on a command line or in an index URL: the runner streams its logs to the API and
the UI, and pip prints the index it is fetching from.

**Python always gets a virtualenv**, because there is no ambient alternative: the
root filesystem is read-only, so `site-packages` cannot be written to, and pip is
removed from the image deliberately — its vendored bundle is what image scanners
report. `python -m venv` restores a working pip from the stdlib.

Where that venv goes is the program's choice. Declare `options.virtualenv` in
`Pulumi.yaml` and it is built exactly there, because Pulumi runs that interpreter
and ignores anything else; declare none and Terrapod builds one under `/tmp` and
points Pulumi at it.

**Go is fetched its modules by a shim, and this is worth knowing.** The `go`
command will talk plain HTTP to a module proxy, but it will never carry a
credential over one -- not in the `GOPROXY` URL, not from a `.netrc`, and not
from a `GOAUTH` helper, whose header it drops in silence. Since the runner
reaches the API over an in-cluster HTTP URL in many deployments, and every
package-cache route requires authentication, Go could otherwise reach the proxy
and never use it.

So Terrapod runs a small listener on `127.0.0.1` inside the Job for the length of
the install, points `GOPROXY` at it, and lets it add the run's token on Go's
behalf. Go carries no credential and so has nothing to refuse. The token travels
exactly the hop it already travels for that run's artifacts, its state and its
binaries -- Go's rule is simply stricter than the one the rest of the Job lives
by. Nothing is exposed outside the pod: the listener is loopback-only and serves
GET alone.

Two further Go settings are forced, both because a sealed deployment has no
second upstream: `GOTOOLCHAIN=local`, so a `go.mod` naming a newer toolchain does
not fetch one, and `GOSUMDB=off`. The module hashes in the program's own `go.sum`
are still verified; it is the transparency-log lookup that is turned off.

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

### Where an agent run's state lives

An agent-mode run points the CLI at Terrapod's own Pulumi service surface — the
same surface a local `pulumi login` against Terrapod talks to — and drives the
ordinary update lifecycle against it: begin, checkpoint, complete. The Job holds
no backend of its own, and the runner is never handed the whole deployment with
its secrets opened — the service seals and opens them a value at a time, as it
does for a laptop.

**State is written continuously and published once.** Pulumi checkpoints all the
way through an update, and each checkpoint is stored durably the moment it
arrives — nothing is buffered in the Job waiting for the end. What is deferred is
*publication*: a checkpoint is held against its update and becomes a state
version only when the update ends, so one update leaves one state version behind
whatever its status, exactly as one Terraform apply does. An update that changed
nothing leaves none, and a preview never checkpoints at all.

Publication is deferred so that a reader never sees a half-applied state. That
matters because another stack's `StackReference` resolves against Terrapod, and
would otherwise build on outputs that are about to change.

Two things follow that a backend private to the Job could not offer:

- **A repository that commits `secure:` config values works.** Those values are
  sealed by the service, and the CLI opens them through it.
- **`StackReference` reads work across stacks**, authorized by the producer
  workspace's remote-state consumer allowlist — the same grant that authorizes
  `terraform_remote_state`. See [Remote state](remote-state.md).

## A `pulumi up` from your own machine shows in run history

When you `pulumi login` against Terrapod and run `pulumi up` from your own
machine, the update becomes a run in the workspace's history: who ran it, when,
what came of it, and the state version it produced.

This is more than the Terraform path gives you, and the reason is the protocol
rather than a preference. Pulumi's CLI drives its backend through a
begin/checkpoint/complete lifecycle, so Terrapod knows when your update starts
and when it ends. Terraform in local mode tells Terrapod nothing until it
pushes the finished state, so there is no run to record — only a state version.

Three things follow that are worth knowing:

- **Previews are not recorded.** A `pulumi preview` changes nothing, writes no
  state version, and cannot checkpoint. A preview also runs constantly while
  you are working, so recording each one would bury the updates in noise.
- **The run holds the workspace while it runs.** It is an apply in progress, so
  Terrapod treats it as one: an agent run will not start against the stack
  meanwhile, and drift checks are skipped until it finishes. That is the same
  serialisation a Terraform CLI apply already gets from the workspace lock.
- **If your CLI dies, the run ends with it.** There is no Kubernetes Job behind
  this run, so what tells Terrapod you are still there is the update's lease,
  which your CLI renews as it works. When the lease lapses the run is marked
  errored and the stack is released — by the same sweep that already promotes
  an abandoned update's last checkpoint.

## What the workspace shows about a stack's state

A Pulumi workspace's state views work from the deployment Terrapod stores, so
the state tab shows the stack's resources and its outputs.

- **The resource graph** is built from the deployment's URNs: one node per
  resource, wired by each resource's `dependencies` and by its `parent` where
  that parent is a real resource. The root `pulumi:pulumi:Stack` is not drawn —
  it is the stack itself rather than infrastructure, and since everything in the
  stack parents to it, drawing it would produce a single hub every other node
  points at.
- **Stack outputs** are shown with secrets masked: an output sealed by the
  stack's secrets provider is reported as present without being revealed, which
  is the same treatment a sensitive Terraform output gets.
- **`pulumi stack ls` reports real resource counts**, recorded when each state
  version is written rather than by reading every stack's whole deployment to
  print one number.

**Cost estimation and the AI run summary work on a Pulumi run.** A preview's
steps are translated into the shape the pricing engine reads, so a previewed
resource is priced wherever its type maps to one the sheet knows, and the
summary is given the preview as a preview — it talks about URNs and updates
rather than hunting for a Terraform plan's `resource_changes`. Resources whose
Pulumi type has no Terraform equivalent are reported unpriced rather than
guessed at.

**The AI architecture critique is still not shown on a Pulumi workspace.** It
reasons over Terraform state, which a Pulumi deployment is not, so the tab is
absent rather than present-and-failing.


## Drift detection

Drift detection works on a Pulumi workspace the same way it does on a Terraform
one: enable it per workspace, and Terrapod queues a plan-only run on the
interval, then sets the workspace's drift status from what that run found.

The check is **`pulumi preview --refresh`**. The refresh is the whole point:
`pulumi preview` on its own compares your program against the state Pulumi has
stored, so a resource someone changed in the cloud console still matches that
stored state and the preview reports nothing. `--refresh` reads each resource
back from its provider first, so the comparison is against the world. Refresh is
already Terrapod's default for every run, so a drift run gets it without asking.

`--expect-no-changes` is deliberately not used, though it is the more obvious
flag. It makes the CLI exit non-zero when anything differs, which would file
every drifted workspace as an **errored** run rather than a **drifted** one, and
the badge exists to tell those apart. The preview's own change report is the
signal instead.

Two consequences worth knowing:

- **Drift-ignore rules do not apply to Pulumi workspaces**, and the field is
  hidden on them. The rules are globs over Terraform attribute paths
  (`aws_instance.web.tags.LastScanned`), and a Pulumi preview produces no such
  document — so a rule written there would silently match nothing. Until they
  are defined in URN terms, a Pulumi workspace reports drift unfiltered.
- **Refresh noise counts as drift.** On Terraform, a provider that rewrites a
  timestamp on every read is what drift-ignore rules exist to suppress; without
  them, a Pulumi stack whose provider does that reads as permanently drifted.
  If that is your stack, the honest answer today is to leave drift detection off
  on it rather than to learn to ignore the badge.

## Where Pulumi is not coerced, and why

- **A Pulumi agent apply is coupled to API availability; a Terraform one is
  not.** Pulumi has no defer-writes mode: the CLI checkpoints to its backend as
  it goes, so an interruption in the middle of an apply can fail an update that a
  Terraform run — holding `terraform.tfstate` in the Job and pushing it once at
  the end — would have survived. That is a characteristic of Pulumi rather than
  something Terrapod chooses, and it is accepted rather than worked around: the
  alternative, a second state path private to the Job, cost more than it bought.
  A Pulumi agent apply wants a stable path to the API.
- **OPA policy sets apply; security scanning does not yet.** A preview produces
  no Terraform plan JSON, so Terrapod builds an OPA input from the engine event
  log instead: each resource's operation, type, URN, declared inputs and changed
  property paths. Applicable sets are evaluated before the preview is reported,
  and a mandatory failure holds the run exactly as it would on a Terraform
  workspace. The input is **not** interchangeable with Terraform's — see
  [`docs/policies.md`](policies.md#the-pulumi-input) before porting a rule.
  Checkov and Trivy still read plan JSON, so scanning is refused on a Pulumi
  workspace rather than holding every apply for a result that cannot arrive
  (#1569); the run says so in `meta.not-evaluated-reason`.

## See also

- [`docs/pulumi-cli-surface.md`](pulumi-cli-surface.md) — the slice of Pulumi's
  service protocol Terrapod implements, which serves both `pulumi login` from a
  laptop and the CLI inside a runner Job.
- [`docs/remote-state.md`](remote-state.md) — the consumer allowlist that
  authorizes a `StackReference` between two stacks.
- [`docs/policies.md`](policies.md) and
  [`docs/security-scanning.md`](security-scanning.md) — the gates, and what they
  currently do on a Pulumi workspace.
- [`docs/autodiscovery.md`](autodiscovery.md) — discovering stacks from a
  monorepo, and how the rename/delete lifecycle reasons per stack rather than
  per directory.
