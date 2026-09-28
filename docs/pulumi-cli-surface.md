# The surfaces the Pulumi CLI consumes

The Pulumi-side counterpart to [`galaxy-cli-surface.md`](galaxy-cli-surface.md)
and [`tfe-cli-surface.md`](tfe-cli-surface.md). Two surfaces, captured
separately:

1. **the plugin download surface** — what the CLI asks a plugin server for
   (below);
2. **the service surface** — what it asks a *state backend* for once you
   `pulumi login <https-url>`, which is the analogue of Terraform's `cloud {}`
   block. That is [its own section](#the-service-surface).

Captured from a real client rather than read from documentation
(`scripts/pulumi-capture.py`), for the reason the Galaxy work established —
five of that surface's findings contradicted the obvious reading of the docs,
and two were invisible to any synthetic test.

**Captured with:** `pulumi` v3.145.0.

## The whole protocol is one request

```
GET {override}/pulumi-resource-random-v4.16.3-linux-amd64.tar.gz
```

That is it. There is no index, no metadata call and no version list, because the
CLI already knows the kind, name, version, OS and architecture before it asks.
The template is literally `pulumi-%s-%s-v%s-%s-%s.tar.gz` inside the binary.

**So nothing here needs a TTL.** The cache-expiry rule turns on whether a thing
can change upstream: a plugin at a version is immutable, and this proxy serves
nothing else. There is no mutable listing to go stale because the client never
asks for one — worth stating plainly, since every other proxy Terrapod runs has
one and the asymmetry looks like an omission otherwise.

Language plugins (`pulumi-language-python-v…`) use the same shape and are served
by the same path.

## Pointing the CLI at it

`PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES` takes a comma-separated `pattern=url`
list:

```sh
export PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES=".*=https://x:$TERRAPOD_TOKEN@terrapod.example.com/api/v1/package-cache/pulumi"
```

### Two traps, both captured

**An anchored `^name$` pattern silently does nothing.** The pattern is an
unanchored regex search against a string wider than the plugin's name, so
`^random$` never matches — and when nothing matches, the CLI falls back to
`get.pulumi.com` **without a warning**. The install succeeds, which is precisely
the failure this proxy exists to prevent: it looks like it is working right up
until someone has no route out.

Measured against `pulumi plugin install resource random`:

| pattern | result |
|---|---|
| `.*` | matches — everything through Terrapod |
| `random` | matches |
| `random$` | matches |
| `^random$` | **silently falls back upstream** |
| `^pulumi-resource-random$` | **silently falls back upstream** |
| `aws` | does not match (correctly — different plugin) |

Use `.*` to route everything, or a bare unanchored name per plugin. Do not
anchor with `^`.

**Credentials go in the URL.** The CLI sends no `Authorization` header of its
own, but userinfo in the override URL becomes one:
`http://x:TOKEN@host` produces `Authorization: Basic …`. Terrapod's
credential parsing already accepts Basic and takes the password, so the username
is ignored — `x` above is a placeholder.

## Version resolution is a separate concern

An **unpinned** program does not just download a plugin; it first resolves what
"latest" means, and that resolution does **not** go through the plugin download
URL:

```
error: could not find latest version for provider random
```

So a program intended to build without upstream access must pin its plugin
versions — in Pulumi YAML via `options.version`, and in the language SDKs by
pinning the provider package. That is a property of the CLI, not something this
proxy can supply, and pinning is good practice in an air-gapped estate anyway.

## Integrity

Upstream publishes no digest alongside the tarball, so Terrapod records none.
This is weaker than the PyPI and npm proxies, where the client checks our bytes
against a digest upstream published, and it is worth being plain about rather
than implying a check that does not happen. Pulumi's own verification is that
the plugin unpacks and runs.

## The stored secrets-provider URL is the one the CLI uses (#1580)

A stack sealed by Terrapod carries a `service` secrets-provider block:

```json
{"type": "service",
 "state": {"url": "…/api/v1/pulumi", "owner": "default", "project": "p", "stack": "dev"}}
```

**The CLI takes that `url` literally and never checks it against the backend it
is logged in to.** From Pulumi's own source: `NewServiceSecretsManagerFromState`
unmarshals the stored state and passes `s.URL` straight to
`getServiceSecretsAccount`, which looks up the saved credential **for that exact
URL**. There is no comparison and no mismatch error — it fails later with:

```
could not find access token for <url>, have you logged in?
```

That is why a stale URL here is not cosmetic. An agent run writes into this block
whatever address the runner reaches the API on, which in many deployments
resolves only inside the cluster (`http://terrapod-api:8000/…`), and nothing on
the write path rewrites a block that already exists. An operator cannot clear the
resulting error by logging in, because the address does not resolve outside the
cluster at all.

**Terrapod normalises the URL on the way out, not in storage.** `stack export`
— which is also how the CLI reads state before an update — serves the block
naming `{external_url}/api/v1/pulumi`. Doing it on read fixes every existing
stack at once, including ones that will never take another write, and changes
nothing stored, so it stays reversible by configuration.

**With no `external_url` configured the block is served exactly as stored.**
Without it the deployment has not declared the address it answers at, and the
only fallback is whichever host the caller happened to use — normalising to a
guess could replace a reachable address with a worse one.

One consequence worth stating plainly: a stack whose block names some *other*
reachable address is normalised to `external_url` too. That operator is not
stranded — `external_url` is by definition where the deployment answers, so
`pulumi login` against it works.

## Deliberately not implemented

Publishing private plugins. The gate asks for `pulumi plugin install` to succeed
with the override set; a publish path can follow if a real need appears, and it
would want the same design conversation the Galaxy one did.

The Pulumi *service* API — `pulumi login` against Terrapod, stacks, state — is a
separate and much larger surface, catalogued and scoped on its own when the
Pulumi engine work reaches it (#1407 §12 phase 4).

## Reproducing the capture

```sh
python3 scripts/pulumi-capture.py [path-to-pulumi]
```

Set `PULUMI_HOME` to a fresh directory when testing by hand, or an
already-installed plugin makes the capture look shorter than it is.

---

# The service surface

What `pulumi` asks a state backend for after `pulumi login <https-url>` — the
analogue of Terraform's `cloud {}` block, and the surface #1407 §2 scopes to
"only the slice the CLI consumes".

Captured with `scripts/pulumi-service-capture.py` against **pulumi v3.261.0**,
by pointing the real CLI at a request-logging stub and answering it until a full
`up` completed: **106 requests, 19 distinct endpoints**. A stub that 404s teaches
only that the client gave up, so each response was filled in until the CLI walked
further — the same method as the four protocol captures before it.

## The endpoints

`{stack}` below is always the triple `{org}/{project}/{stack}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/user` | login + `whoami`; returns the org list |
| GET | `/api/user/organizations/{org}` | org lookup; called constantly |
| GET | `/api/capabilities` | feature negotiation; `{"capabilities": []}` is accepted |
| GET | `/api/user/stacks?project=` | `stack ls` |
| POST | `/api/stacks/{org}/{project}` | `stack init` — **always refuses**; see below |
| GET | `/api/stacks/{stack}` | stack lookup; 404 means "does not exist" |
| DELETE | `/api/stacks/{stack}` | `stack rm` |
| GET | `/api/stacks/{stack}/export` | **read state** |
| POST | `/api/stacks/{stack}/import` | **write state wholesale**; gzipped; async |
| POST | `/api/stacks/{stack}/encrypt` | **encrypt a secret** — body `{"plaintext"}` |
| POST | `/api/stacks/{stack}/decrypt` | decrypt one |
| POST | `/api/stacks/{stack}/batch-decrypt` | decrypt many |
| POST | `/api/stacks/{stack}/preview` | begin a preview |
| POST | `/api/stacks/{stack}/update` | begin an update |
| POST | `/api/stacks/{stack}/refresh` | begin a refresh |
| POST | `/api/stacks/{stack}/destroy` | begin a destroy |
| POST | `/api/stacks/{stack}/update/{updateID}` | **start** it; must return a lease token |
| GET | `/api/stacks/{stack}/update/{updateID}` | poll status (used by `stack import`) |
| PATCH | `/api/stacks/{stack}/update/{updateID}/checkpoint` | **write state**; gzipped. Held against the update; the last one becomes its state version (#1564) |
| POST | `/api/stacks/{stack}/update/{updateID}/events/batch` | engine events; gzipped |
| POST | `/api/stacks/{stack}/update/{updateID}/complete` | end it — `{"status":"succeeded"}` |
| POST | `/api/stacks/{stack}/update/{updateID}/renew_lease` | extend the lease |

**Three of those rows were not exercised by this first capture.** #1571 has since exercised two of them; see [the secondary commands](#the-secondary-commands-1571):

* `renew_lease` — **now observed**, with body `{"token": "", "duration": 300}` part-way through a long update. The CLI adopts the token in the response, and Terrapod's renewal returns none, which breaks every long update (#1562).
* `batch-decrypt` — **now observed**. Every state command and every read of a deployment with secrets calls it, and Terrapod's map-shaped response works.
* `decrypt` (the single-value form) — still not observed.

## Who may call what

Every stack is a Terrapod workspace, and every call addressed at one is authorized
against that workspace exactly as the Terraform routes are (#1550): owner, label RBAC,
platform roles and the `everyone` floor all apply unchanged. What each call needs:

| Call | Requires |
|---|---|
| `stack ls` | `workspace:read` on each stack — stacks you cannot read are not listed |
| stack lookup | `workspace:read` |
| `stack rm` | `workspace:delete` |
| `stack export` | `state:read` |
| `stack import` | `state:write` |
| `encrypt`, `decrypt`, `batch-decrypt` | `state:read` — decrypt returns secret values |
| `preview` | `run:plan` |
| `up`, `refresh` | `run:apply` |
| `destroy` | `run:apply-destroy` |
| starting an update | whatever beginning it required — read from the update, not the URL |
| polling an update | `run:read` |
| cancelling an update (`pulumi cancel`) | whatever beginning it required — read from the update, not the URL |

Without `workspace:read`, every call answers exactly as it would for a stack that does
not exist — the same 404 and the same message — so stack names cannot be probed. With
read access but without the capability a call needs, the answer is a 403 naming it.

The in-update calls (`checkpoint`, `events/batch`, `complete`, `renew_lease`) carry a
lease rather than a user. A lease authorizes one update on one stack: it is refused
against any other stack, and it is checked before the stack is looked up, so a caller
without a valid lease learns nothing about which stacks exist. A preview's lease cannot
`checkpoint` — a preview never writes state, and allowing it would let `run:plan` buy
`state:write`.

**A local update holds the workspace lock (#1562).** An `up`, `refresh` or `destroy`
from a CLI logged in to Terrapod takes the same workspace lock a Terraform CLI apply
takes, for as long as the update runs:
- The stack shows as locked in the UI, and the run dispatcher will not start an agent
  apply against it meanwhile.
- A workspace that is already locked, manually or by another update, refuses the update
  with a 409 naming the lock.
- An update is also refused while an agent run's apply is in progress on the stack.
- A VCS-connected agent workspace refuses a local update entirely, as Terraform refuses
  a CLI apply there, because its changes come from the repository. It can still be
  previewed.

Previews take no lock, so any number can run at once. The lock is released when the
update completes or is cancelled with `pulumi cancel`. If the CLI dies instead, its lease
lapses after 30 minutes without renewal, and a periodic sweep releases the lock within a
minute of that. An operator can also clear it with force-unlock, as for any lock a
crashed CLI leaves behind.

**One state version per update (#1564).** An update checkpoints the stack many
times as it runs. Each checkpoint replaces the one before it and is held against the
update, and the last one becomes a single state version when the update ends:
- when it completes, whatever its status. A failed update keeps its partial state,
  because that is the only record of what it created;
- when it is cancelled with `pulumi cancel`;
- when its CLI dies, through the same sweep that releases its lock.

An update that changes nothing leaves no version behind. Agent runs drive this same
surface (#1881), so they take this path too — one state version per update, from the
same held checkpoint — and the two modes match because they are the one mechanism
rather than two that agree. Each version records its size, md5, sha256 and who made
it, and the current one cannot be deleted, as for Terraform.

**Holding the checkpoint is what makes a live backend safe for an agent run.**
State is written continuously — every checkpoint is stored durably as it arrives,
and nothing waits in the Job — while *publication* happens once, at the end. A
reader therefore never sees a half-applied state, which matters because another
stack's `StackReference` resolves against Terrapod and would otherwise build on
outputs that are about to change. A preview's lease cannot checkpoint at all, so a
preview writes no state however it is run.

**`pulumi stack rm` can be undone.** It goes through the same delete as the UI and API,
so the stack is listed under deleted workspaces and can be restored with its state
history. A deployment carries no serial of its own, so a restored stack numbers its
versions from 1, oldest first.

**Manual upload takes `pulumi stack export` output.** Upload the file as it is to a
Pulumi workspace (`POST /api/v1/workspaces/{id}/state-versions/actions/upload`). It is
stored unwrapped and exports back unchanged. An export made with `--show-secrets` is
refused, because storing it would put the stack's secrets in state in the clear; load
that one with `pulumi stack import`, which seals them first.

**A runner token is allowed, on terms of its own (#1880).** This surface serves both
the CLI on a laptop and the CLI inside a runner Job, because an agent run drives it as
its backend (see
[how Terrapod runs Pulumi on an agent](#how-terrapod-runs-pulumi-on-an-agent)). A
runner token is not a weak user but a capability bound to one run: it carries the
`everyone` role and nothing else, so resolving it through role RBAC would grant it
nothing at all. What it may do is therefore stated outright, keyed on the run it
belongs to:

| Where the call is addressed | What the run's token may do |
|---|---|
| The run's own workspace | read and preview; update as well, but **only** if the run is apply-capable — a plan-only run cannot begin an update, or a speculative PR plan could apply from inside a Job running the author's own program |
| A destroy run's own workspace | the above, plus destroy |
| Any other workspace | read, and only where that workspace's remote-state consumer allowlist names this run's workspace — which is what authorizes a `StackReference` exactly as `terraform_remote_state` is authorized |

`workspace:delete` is granted in no case, so `pulumi stack rm` from inside a Job is
refused however the program asks for it. The Job runs arbitrary program code, and
letting it delete the workspace it is running in is a capability nobody asked for.

`stack init` never creates a stack (below), and it reports a name as already taken only
to someone who can read that stack.

## `stack init` does not create a stack

`POST /api/stacks/{org}/{project}` is served, but it always refuses: a 404 whose
message names where workspaces come from.

Terrapod workspaces are created in the UI, with the Terraform provider, or via
`POST /api/v1/workspaces` — never by an engine's own CLI. Terraform's CLI has
never created one (`init` looks a workspace up and fails if it is absent), and a
CLI that could would be bringing a platform resource into being with no RBAC
review and no record of where it came from.

So the flow is: create the workspace with `engine = "pulumi"`, then

```sh
pulumi stack select default/{project}/{stack}
```

**Name it `project::stack`.** A Pulumi stack is identified by
`{org}/{project}/{stack}` and a workspace has one flat name, so the two halves
are joined with `::` — a sequence neither a Pulumi project nor a stack admits.
That composed name is what `stack select` resolves to, so a workspace named
anything else is invisible to the CLI. Creating one with a single-part name is
rejected, rather than accepted and then never found.

```hcl
resource "terrapod_workspace" "app_dev" {
  name   = "app::dev"     # pulumi stack select default/app/dev
  engine = "pulumi"
}
```

The route stays mounted rather than being removed so the refusal can carry that
instruction — the CLI prints the message verbatim, and an unmounted route would
give the operator a bare 404 with nothing to act on.

## Pulumi workspaces in the UI

A Pulumi workspace is listed, opened and edited like any other (#1554, #1555), through the native `/api/v1/workspaces` routes. The TFE-compatible surface serves Terraform alone.

- **List.** A Pulumi row carries a *Pulumi* badge, and its `project::stack` name is shown as `project / stack`. An engine filter appears when more than one engine is enabled.
- **Page.** It shows the engine and hides the settings that only mean something to Terraform: execution backend, version, Terragrunt and var files. It adds **Lock the update to the approved preview** (`pulumi-bind-plan`, off by default; see [#1553](https://github.com/mattrobinsonsre/terrapod/issues/1553)).
- **Create.** The form offers an engine only when more than one is enabled (`GET /api/v1/engines`). For Pulumi it asks for a `project::stack` name, which you then select with `pulumi stack select default/{project}/{stack}`.

A deployment with only Terraform enabled shows none of this: no badge, no filter and no engine picker.

## Findings

**Auth is `token <value>`, and it changes mid-run.** Not `Bearer`. Everything
addressed at a stack uses `Authorization: token <api-token>` — but the three
calls made *during* an update (`checkpoint`, `events/batch`, `complete`) use
`Authorization: update-token <lease>` instead, with the lease handed out when the
update is started. Two schemes on one surface, and the second is invisible to any
test that supplies its own authenticated client.

**The service is the stack's secrets provider.** `POST .../encrypt` is not
optional colour: in `httpstate` mode the CLI delegates secret encryption to the
backend and calls it during a plain `up`. This is a real obligation that "state
backend" does not imply — Terrapod would have to run an encrypt/decrypt oracle
per stack, and own the key that makes stack state readable.

**State is a whole document, not a delta.** It is read with `GET .../export` and
written with `PATCH .../update/{id}/checkpoint`, each carrying the entire
deployment. Terrapod's existing state-version storage therefore fits without a
new merge model. Checkpoint and event bodies are **gzipped**
(`Content-Encoding: gzip`), so a handler must decompress rather than parse the
raw body.

**A preview is an update.** `preview` creates an update and then starts it via
`POST .../update/{updateID}` — the same path an `up` uses. There is no separate
preview lifecycle, and the start call **must return a lease token**: without one
the CLI aborts with `fatal: An assertion has failed: persisted actions require a
token`.

**An update has an explicit begin and end**, so a run that dies mid-update leaves
the stack with a started-but-never-completed update — which is exactly what the
lease exists to time out.

**Concurrency is enforced by refusing to start.** There is no lock endpoint. A
`409` on the begin call ends the CLI immediately — exit 1, no retry, no wait —
and the service's `message` field is printed verbatim:

```
error: [0] another update is currently in progress
```

So Terrapod's existing per-workspace run serialisation maps onto this directly:
refuse the begin, and put the explanation in `message`.

**An empty stack's deployment is `null`.** `{"version": 3, "deployment": null}`.
A synthetic empty deployment (`{}`, or a manifest with no resources) fails the
CLI's snapshot integrity check.

**The surface does not have to sit at the root.** The CLI appends `/api/...` to
whatever base URL it was given, path prefix included — verified by logging in to
`http://host/api/v1/pulumi` and watching it request
`/api/v1/pulumi/api/user`, then serving it successfully from there. This
is the question #1484 had to settle for NuGet and it lands the opposite way:
Terrapod can mount this natively rather than at the root, which it reserves for
the two surfaces genuinely forced there (`/v2/` and `/.well-known/terraform.json`).

## Remote execution is not on this surface — but it does exist

Nothing above executes anything. The service is a state store, a secrets oracle and an
event sink; the CLI runs the language host, the engine and the providers locally, and
`events/batch` is the CLI *pushing* what it did rather than a remote run streamed back.
The `preview` and `update` bodies carry the project's name, runtime, options and config —
**never the program source** — so the service could not execute it even in principle.

Do not read that as "Pulumi has no remote execution". It does:
`pulumi deployment run <operation>` queues a job on Pulumi Cloud, takes
`--agent-pool-id`, and can stream its logs back (`--suppress-stream-logs`, default true).
That lives on the **Deployments** API, a separate surface from this one, which is why a
capture of `login` / `preview` / `up` never touches it.

It is also not the same shape as `terraform apply` against an agent. Terraform uploads
the local working directory, so uncommitted edits execute remotely; `deployment run` is
git-sourced (`--git-branch` / `--git-commit` / `--git-repo-dir` / `--git-auth-*`), so it
deploys committed code and local edits do not participate.

## How Terrapod runs Pulumi on an agent

The section above is about what *Pulumi's own CLI* can drive remotely. Terrapod's
agent execution is a different thing and does not use the Deployments API at all:
a run is queued in Terrapod, a listener launches a Kubernetes Job, and the Job
runs `pulumi preview` then `pulumi up` against the fetched configuration — the
same shape as a Terraform run, and the same one an operator selects with
`execution_mode = "agent"`. **The engine does not decide where a run executes.**

What the Job arranges that is worth knowing about as an operator:

**Terrapod is the run's backend (#1881).** The Job holds no backend of its own: it
points the CLI at the service surface above and drives the ordinary update
lifecycle against it, with the run's own short-lived token. There is no separate
state path for an agent run to take.

1. **At the start**, the Job selects the run's stack —
   `pulumi stack select default/{project}/{stack}`. Nothing creates it: a stack is
   a Terrapod workspace, so a stack this run names but Terrapod does not have
   fails here, at the start, rather than part-way through a preview.
2. **The preview reads state through `export`** and writes none. A preview's lease
   cannot checkpoint, so this holds however the preview is driven.
3. **The update checkpoints as it goes.** Each checkpoint is stored durably when it
   arrives and replaces the one before it, held against the update rather than
   published. `complete` is what promotes the last one to the workspace's single
   new state version, linked to the run (#1564). An update that changed nothing
   leaves no version behind.
4. **A failed update keeps its last checkpoint**, for the same reason a failed
   Terraform apply keeps its partial state: it is the only record of what the
   update created. So is an abandoned one — if the Job dies, the lease lapses and
   the sweep promotes what it wrote.

**Why a live backend is safe here**, since it was once thought not to be. #1576
moved these runs to a file backend inside the Job, reasoning that Pulumi
checkpoints continuously and a live backend would therefore move the workspace's
state mid-run with no decision point. The property that actually matters is
narrower — an apply's state must not be *published* until something decides to
publish it — and holding the checkpoint satisfies it. State is written
continuously and published once, which is the Terraform principle reached by a
different mechanism.

**The cost, accepted.** An agent apply is coupled to API availability in a way a
Terraform apply is not. Pulumi has no defer-writes mode, so an interruption in the
middle of an apply can fail an update that a Terraform run — holding
`terraform.tfstate` in the Job and pushing it once — would have survived. That is
a characteristic of Pulumi rather than a Terrapod defect, and a Pulumi agent apply
wants a stable path to the API.

What this means for a program:

- **A committed `Pulumi.<stack>.yaml` is used as it stands.** Nothing is stripped
  from the Job's working copy. The file-backend model had to remove the
  `encryptionsalt`, `secretsprovider` and `encryptedkey` lines, because they named
  a provider the Job's own stack did not use; one backend with one secrets
  provider has nothing to reconcile.
- **`secure:` values in that file work**, closing the limitation #1577 tracked.
  They are sealed by the service, and the CLI opens them through it.
- **`StackReference` works**, closing #1578. A read of another stack is authorized
  by that workspace's remote-state consumer allowlist — the same grant that
  authorizes `terraform_remote_state` — and a stack the allowlist does not name is
  refused.
- **A stack whose secrets are sealed by a passphrase or cloud KMS** — one moved
  there with `pulumi stack change-secrets-provider` — still cannot be run on an
  agent. Those secrets are sealed under a key only the operator's CLI holds, and
  the Job has no more access to it than Terrapod does.
  `pulumi stack change-secrets-provider default` moves the stack back.
- **`PULUMI_BACKEND_URL` set as a workspace variable is overridden.** Agent mode
  owns the backend, as it does Terraform's, so the run's own backend settings are
  applied after the workspace's variables and cannot be redirected by one.

**The binary is fetched, not baked in.** `pulumi` is pulled through the same
cache that serves `tofu`/`terraform`, and the version is the workspace's
`engine-version` — per workspace, exactly as a Terraform version is. Partial
versions work: `3.208` means the newest `3.208.*`. A workspace that pins none
gets `default_pulumi_version` from your values. If the cache cannot supply the
binary the run fails rather than falling back to whatever `pulumi` might be on
the image.

**Nothing reaches for Pulumi Cloud.** The backend is Terrapod, set through
`PULUMI_BACKEND_URL` and `PULUMI_ACCESS_TOKEN` — together the env-var form of
`pulumi login` — so the Job performs no login step and writes no credentials under
`$HOME`. Plugin downloads are redirected to Terrapod's package cache, which the
same short-lived token authenticates to. Both matter most in an air-gapped
deployment, where the CLI's defaults would otherwise reach for `app.pulumi.com`
and `get.pulumi.com` and simply hang.

**The update is not bound to the preview unless you ask.** By default the
preview saves nothing and the update is a plain `pulumi up`, which works out its
changes afresh — the way Pulumi is normally run in CI, with the preview there for
a person to review. A workspace can opt in with `pulumi-bind-plan` (#1553): the
preview then saves its plan (`--save-plan`), the plan is carried to the update's
pod, and `pulumi up --plan` refuses any operation the approved preview did not
show. A saved plan carries its secrets as ciphertext sealed by the stack's own
secrets provider, and both phases speak to the same one, so the plan opens where
it is read and nothing travels beside it. It is off by default because Pulumi's update plans are still marked
experimental upstream, and an open bug (pulumi/pulumi#17546) makes them fail
spuriously when cloud credentials are resolved during the preview — exactly how
a Terrapod runner gets its credentials. Either way, Terrapod refuses to confirm
an approved run whose stack state has moved since its preview (#647).

## Nothing from the management surface was required

A full `login → stack init → stack ls → preview → up → refresh → export →
destroy → stack rm` cycle completed without touching Deployments, Policy Packs,
Insights, Environments/ESC, Webhooks, Registry or organisation management —
confirming the §2 scope. `/api/capabilities` returning an empty list is accepted.

That capture predates #1535: `stack init` now refuses, and the workspace is
created in Terrapod first with `stack select` in its place. The rest of the
cycle — which is what this section is about — is unchanged.

## The secondary commands (#1571)

The main lifecycle above was captured in #1502. This section covers the rest of what a Pulumi user runs day to day. It was captured with `scripts/pulumi-secondary-capture.py` against CLI v3.262.0, in two passes:

- **Stub pass:** a recording stub, which shows what the CLI sends.
- **Live pass:** a real Terrapod behind its BFF, which shows what Terrapod answers.

"Live" statuses are the ones Terrapod returned on a Tilt stack.

| Command | What the CLI sends | What Terrapod answers today | What it should answer |
|---|---|---|---|
| `stack history` | `GET …/updates?pageSize=10&page=1` | **404** — the command fails | The workspace's runs and updates, newest first |
| `stack tag set` / `tag rm` | `PATCH …/tags`, whose body is the **complete** tag map, including `pulumi:project` and `pulumi:runtime` — it replaces, it does not merge | **404** | A decision on how stack tags relate to workspace labels, then a route |
| `stack tag ls` | nothing new — it reads `tags` from `GET` on the stack | works (labels are returned as tags) | — |
| `stack rename` | `POST …/rename` with `{"newName", "newProject"}` | **404** | A rename of the workspace to `newProject::newName`, subject to the same validation as any rename |
| `state protect`, `unprotect`, `delete`, `edit` | `GET …/export` → `batch-decrypt` → `encrypt` → `POST …/import` → poll `GET …/update/{id}` | **works** — all four round-trip through export and import | — |
| `change-secrets-provider passphrase` | export → `batch-decrypt` → import | **works** | — |
| `change-secrets-provider default` (back to the service) | `encrypt`, then export → import | **Fixed by #1573:** `encrypt` is byte-safe. Before that it returned 500 (see below), and the stack was left on the passphrase provider | — |
| `change-secrets-provider awskms://…` | a KMS call made **by the CLI itself**; the service only sees `GET` on the stack | nothing to serve | Nothing: KMS credentials belong wherever the CLI runs |
| lease renewal | `POST …/update/{id}/renew_lease` with `{"token": "", "duration": 300}`, part-way through an update of a few minutes | **Fixed by #1562:** the lease comes back, and the update record and stack lock are extended to match. Before that, `200 {}` with no token | — |
| `cancel` | `GET` on the stack, to read its `activeUpdate`; then `POST …/update/{activeUpdate}/cancel` with an empty body | **Fixed by #1562:** the stack reports `activeUpdate` while an update runs, and the cancel route ends it and releases the stack. Before that, the CLI stopped at "stack has never been updated" | — |

**Three findings the table understates.**

- **Every update longer than a few minutes fails.** The CLI renews its lease part-way through and uses the token in the response. Terrapod's renewal returns none, so every call after it carries an empty lease and gets a 401. The update then ends with "this command requires logging in". Its `complete` never lands, so the stack lock stays held until the lease runs out: 30 minutes in which the next update is refused with a 409. The live pass hit exactly this on a 200-second update. **Fixed by #1562**, which returns the lease from the renewal.
- **`encrypt` is not byte-safe.** The CLI encrypts binary values, not only text. Terrapod decodes the plaintext as UTF-8 with `surrogateescape`, and the encryption layer then fails to encode it. The result is a 500 (`UnicodeEncodeError`) that the CLI retries four times at the start of every `up`, and a hard failure when moving a stack back to the service's own secrets provider. `decrypt` has the mirror-image problem. **Fixed by #1573:** values are sealed as bytes. The encryption layer is handed their base64, and `decrypt` and `batch-decrypt` return the exact bytes. The sealed value is marked outside the envelope (`terrapod-bytes:v1:`), so anything sealed before the fix is still read the old way.
- **A Pulumi workspace cannot be deleted through the native API.** `DELETE /api/v1/workspaces/{id}` still goes through the Terraform-only lookup, so it answers 404. That is how the live pass's cleanup failed. It is the delete half of #1554.

The passphrase and cloud-KMS providers can no longer be chosen at `stack init`, which refuses (#1535). They are reached with `change-secrets-provider` on a workspace that already exists, and the rows above cover that path.

## Reproducing the service capture

```sh
python3 scripts/pulumi-service-capture.py                 # the main lifecycle
python3 scripts/pulumi-secondary-capture.py               # the secondary commands, against a stub
python3 scripts/pulumi-secondary-capture.py \
  --backend https://terrapod.local --token "$TOKEN"       # …and what a real Terrapod answers
```

The live pass creates the workspace `proj::capture` through the native API and
tries to delete it afterwards. Until the native delete serves Pulumi workspaces,
that clean-up fails and the workspace has to be removed by hand.

Requires Docker. Re-run it against a new CLI rather than assuming this still
holds; that is what the script is for.
