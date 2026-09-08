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
| POST | `/api/stacks/{org}/{project}` | `stack init` — body `{"stackName", "tags"}` |
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

**Three of those rows were not exercised by the capture** and are listed from the
CLI's behaviour rather than observed traffic — treat their shapes as unconfirmed:

* `renew_lease` — the runs were too short to need it, but the CLI holds a lease
  for the life of an update.
* `decrypt` and `batch-decrypt` — the captured program had no secret *config* to
  read back. `encrypt` was called six times during an ordinary `up`, so the
  provider obligation itself is confirmed; only the read direction is not.

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

## Nothing from the management surface was required

A full `login → stack init → stack ls → preview → up → refresh → export →
destroy → stack rm` cycle completed without touching Deployments, Policy Packs,
Insights, Environments/ESC, Webhooks, Registry or organisation management —
confirming the §2 scope. `/api/capabilities` returning an empty list is accepted.

## Reproducing the service capture

```sh
python3 scripts/pulumi-service-capture.py
```

Requires Docker. Re-run it against a new CLI rather than assuming this still
holds; that is what the script is for.
