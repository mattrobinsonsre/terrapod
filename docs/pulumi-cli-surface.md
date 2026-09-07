# The plugin surface the Pulumi CLI consumes

The Pulumi-side counterpart to [`galaxy-cli-surface.md`](galaxy-cli-surface.md)
and [`tfe-cli-surface.md`](tfe-cli-surface.md): what the `pulumi` CLI asks a
plugin download server for, and nothing else.

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
export PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES=".*=https://x:$TERRAPOD_TOKEN@terrapod.example.com/api/terrapod/v1/package-cache/pulumi"
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
