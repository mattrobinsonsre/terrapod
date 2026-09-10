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
| PATCH | `/api/stacks/{stack}/update/{updateID}/checkpoint` | **write state**; gzipped |
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

Without `workspace:read`, every call answers exactly as it would for a stack that does
not exist — the same 404 and the same message — so stack names cannot be probed. With
read access but without the capability a call needs, the answer is a 403 naming it.

The in-update calls (`checkpoint`, `events/batch`, `complete`, `renew_lease`) carry a
lease rather than a user. A lease authorizes one update on one stack: it is refused
against any other stack, and it is checked before the stack is looked up, so a caller
without a valid lease learns nothing about which stacks exist. A preview's lease cannot
`checkpoint` — a preview never writes state, and allowing it would let `run:plan` buy
`state:write`.

**Runner tokens are refused.** This surface serves the CLI in local mode only. An
agent-mode run never uses it: its stack lives in a file backend inside the runner Job,
and its state is handed over through the run's artifacts (see
[how Terrapod runs Pulumi on an agent](#how-terrapod-runs-pulumi-on-an-agent)). Every
call made with a runner token is answered 403, whatever route it names (#1576).

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

**State stays in the Job, as Terraform's does (#1576).** An agent run does not use
Terrapod as a live Pulumi backend, and does not call the service surface above at
all. The Job runs the CLI against a file backend in its own workspace, the way a
Terraform run keeps `terraform.tfstate` beside its configuration:

1. **At the start**, the Job fetches the stack's deployment from
   `GET /runs/{run_id}/artifacts/pulumi-deployment`, with its secrets opened. It
   creates the stack locally with a passphrase that exists only for the life of
   the Job, and imports the deployment. The CLI stores it sealed under that
   passphrase.
2. **The preview and the update run against that local stack.** Nothing is
   written to Terrapod while they run: no leases, no checkpoints, no engine
   events.
3. **After an update**, the Job exports the stack with `--show-secrets` and hands
   it back once, with `PUT` to the same path. Terrapod seals the secrets again
   with its own key and stores the result as the next state version, linked to
   the run. It does this whether or not the update succeeded, because a failed
   `up` can still have created resources. An update that changed nothing hands
   back nothing, and a preview never hands anything back.
4. **If anything else wrote the stack while the run held it**, the hand-back is
   refused and the workspace is flagged state-diverged, as for a Terraform run
   whose state upload fails.

What this means for a program:

- **A committed `Pulumi.<stack>.yaml` keeps working**, with one change: its
  `encryptionsalt`, `secretsprovider` and `encryptedkey` lines are removed from
  the Job's working copy (never from your repository), because they name a
  provider the Job's stack does not use.
- **`secure:` values in that file are not yet supported in agent runs (#1577).**
  They are sealed by the provider the stack used when they were set, and the
  Job's passphrase cannot open them. A run whose stack file holds any fails
  early, with a message saying so. Supply those values as workspace variables
  instead.
- **`StackReference` does not yet work in agent runs (#1578).** It resolves
  against the Job's own backend, which holds only the run's stack.
- **A stack whose secrets are sealed by a passphrase or cloud KMS** — one moved
  there with `pulumi stack change-secrets-provider` — cannot be run on an agent,
  because Terrapod holds no key for it. The run fails, saying which provider is
  in the way. `pulumi stack change-secrets-provider default` moves the stack
  back.
- **`PULUMI_BACKEND_URL` and `PULUMI_CONFIG_PASSPHRASE` set as workspace
  variables are overridden.** Agent mode owns the backend, as it does
  Terraform's.

**The binary is fetched, not baked in.** `pulumi` is pulled through the same
cache that serves `tofu`/`terraform`, so the version is
`registry.platform_tools.pulumi_version` in your values (default `3.208.0`) and
an upstream fix reaches a deployment with a `helm upgrade` rather than a Terrapod
release. If the cache cannot supply it the run fails rather than falling back to
whatever `pulumi` might be on the image.

**Nothing reaches for Pulumi Cloud.** The backend is a directory in the Job, set
through `PULUMI_BACKEND_URL`, so there is no `pulumi login` to perform. Plugin
downloads are redirected to Terrapod's package cache, which the run's own
short-lived token authenticates to. Both matter most in an air-gapped
deployment, where the CLI's defaults would otherwise reach for
`app.pulumi.com` and `get.pulumi.com` and simply hang.

**The update is not bound to the preview unless you ask.** By default the
preview saves nothing and the update is a plain `pulumi up`, which works out its
changes afresh — the way Pulumi is normally run in CI, with the preview there for
a person to review. A workspace can opt in with `pulumi-bind-plan` (#1553): the
preview then saves its plan (`--save-plan`), the plan is carried to the update's
pod, and `pulumi up --plan` refuses any operation the approved preview did not
show. A saved plan carries its secrets sealed under the preview's stack key, so
the key travels with the plan and the update's stack is made with the same one.
That leaves a plan's secrets as exposed as a Terraform plan file leaves its
sensitive values, which it holds in the clear. It is off by default because Pulumi's update plans are still marked
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
| `change-secrets-provider default` (back to the service) | `encrypt`, then export → import | **fails**: `encrypt` returns 500 (see below), and the stack is left on the passphrase provider | Byte-safe encryption |
| `change-secrets-provider awskms://…` | a KMS call made **by the CLI itself**; the service only sees `GET` on the stack | nothing to serve | Nothing: KMS credentials belong wherever the CLI runs |
| lease renewal | `POST …/update/{id}/renew_lease` with `{"token": "", "duration": 300}`, part-way through an update of a few minutes | **200 `{}` — no token** | `{"token": "<lease>"}`, and the stack lock extended to match |
| `cancel` | `GET` on the stack, to read its `activeUpdate`; then `POST …/update/{activeUpdate}/cancel` with an empty body | the CLI stops at "stack has never been updated": `GET` on the stack never reports an `activeUpdate` | `activeUpdate` while an update runs, and the cancel route |

**Three findings the table understates.**

- **Every update longer than a few minutes fails.** The CLI renews its lease part-way through and uses the token in the response. Terrapod's renewal returns none, so every call after it carries an empty lease and gets a 401. The update then ends with "this command requires logging in". Its `complete` never lands, so the stack lock stays held until the lease runs out: 30 minutes in which the next update is refused with a 409. The live pass hit exactly this on a 200-second update.
- **`encrypt` is not byte-safe.** The CLI encrypts binary values, not only text. Terrapod decodes the plaintext as UTF-8 with `surrogateescape`, and the encryption layer then fails to encode it. The result is a 500 (`UnicodeEncodeError`) that the CLI retries four times at the start of every `up`, and a hard failure when moving a stack back to the service's own secrets provider. `decrypt` has the mirror-image problem.
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
