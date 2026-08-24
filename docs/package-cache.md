# Language package proxies (PyPI and npm)

Terrapod proxies PyPI and the npm registry, so a run can resolve its dependency
closure without reaching the internet. Point a runner at them and `pip install`
and `npm install` work inside a sealed network.

```yaml
# The runner's environment
PIP_INDEX_URL: https://terrapod.example.com/api/terrapod/v1/package-cache/pypi/simple
NPM_CONFIG_REGISTRY: https://terrapod.example.com/api/terrapod/v1/package-cache/npm/
```

## Why this exists

Terrapod already caches engine binaries, providers and container images, and the
promise those add up to is that a run needs no upstream reach. Language
dependencies are the gap in that promise, and specifically **Pulumi's**: a Pulumi
program's `package.json` or `pyproject.toml` belongs to *you* and differs per
workspace, so unlike an Ansible collection it can never be baked into a runner
image. `npm install` happens at run time and has to reach a registry.

PyPI earns its place twice over. It also serves Ansible collections' Python
dependencies, and `ansible-builder` when an operator builds execution
environments **inside** the sealed network — the image needs nothing at run time,
but building it runs `pip install`.

## Using it

Both proxies require authentication, including for reads. Any Terrapod
credential works.

**pip** sends Basic auth, so the credential goes in the index URL or `.netrc`:

```sh
pip install --index-url "https://any:$TERRAPOD_TOKEN@terrapod.example.com/api/terrapod/v1/package-cache/pypi/simple" flask
```

**npm** sends a bearer token, configured in `.npmrc`:

```ini
registry=https://terrapod.example.com/api/terrapod/v1/package-cache/npm/
//terrapod.example.com/api/terrapod/v1/package-cache/npm/:_authToken=${TERRAPOD_TOKEN}
```

A runner's own short-lived token works for both, so a run authenticates as
itself rather than carrying a shared credential.

### Why reads need a credential

An unauthenticated package proxy is an open bandwidth relay for anyone who finds
it, serving strangers' installs from your storage and your egress. The list of
what you have cached is also a fair description of what your estate runs.

## Integrity

The proxies do not re-hash or re-sign anything, and are deliberately **not** a
trust boundary.

Only the *URL* in an index is rewritten. Upstream's own integrity metadata —
npm's `dist.integrity` sha512 SRI, PyPI's `#sha256=` link fragments — is passed
through exactly as published. Since Terrapod serves byte-identical content, your
client checks our bytes against the digest the package author published, and a
corrupted or substituted artifact fails at the client where it should.

## Air-gapped operation

Set `registry.cache_only: true` and Terrapod never attempts an upstream request
for anything, these proxies included.

A sealed node serves **an index of what it actually holds**, rather than
upstream's. That distinction is the whole feature: an upstream index lists
versions whose files are not present, the client resolves to one of them, and the
install dies on an error it can do nothing about. An index restricted to cached
artifacts resolves to something installable.

For npm this means the packument is cached alongside the tarballs it describes —
a sealed node cannot fetch one, and without it npm has no dependency ranges and
cannot resolve at all — and is then filtered to the versions actually held,
`dist-tags` included.

Asking a sealed node for something it does not have returns a 404 that names the
setting:

```
requests is not cached and this node is sealed (registry.cache_only).
Warm it before sealing.
```

That is deliberate. A bare 404 sends a developer looking for a package that
exists, and a timeout sends an operator to look at their firewall.

**Warm before you seal.** Run the installs you expect to need with `cache_only`
off — pointing at an internal mirror if you have one — then seal.

## Configuration

| Value | Default | What it does |
|---|---|---|
| `api.config.registry.package_cache.enabled` | `true` | Serve the proxies. |
| `...package_cache.pypi.enabled` / `.npm.enabled` | `true` | Per ecosystem. Disabling 404s the endpoints; cached artifacts stay and reappear if re-enabled. |
| `...package_cache.pypi.upstream` | `https://pypi.org` | Where to pull through to. Point it at an internal mirror to cache that instead. |
| `...package_cache.npm.upstream` | `https://registry.npmjs.org` | As above. |
| `api.config.artifact_retention.package_cache_retention_days` | `30` | Days since **last access** before an artifact is eligible for cleanup. Skipped entirely on a sealed node, where an evicted artifact cannot be re-fetched. |
| `api.config.registry.cache_only` | `false` | Seals every cache, these included. |

The upstream is a single operator-set URL rather than a client-chosen host. A
client names a package, never a registry, so this is the whole of the
request-forgery surface and you own it.

### One setting worth getting right

Set `api.config.external_url`. npm requires an absolute `dist.tarball` URL, and
without `external_url` Terrapod has to infer its own address from forwarded
headers. That works, but the operator's explicit answer is always better than an
inference — and it is the only one a client cannot influence.

PyPI needs no such setting: the simple index resolves relative URLs against the
index page, so those links are correct by construction.

## What is not here yet

**GOPROXY and NuGet.** Both are small additions on the same substrate, for Pulumi
Go and .NET programs.

**A warm-ahead API.** Warming today means running the install you expect to need.
An explicit "cache these packages" endpoint would make preparing an air-gapped
deployment less of a rehearsal.

## Warming ahead of a seal

Warming by *running the thing you expect to need* is a rehearsal: you find out
whether you guessed the set right only after sealing, which is the expensive
moment to discover a gap.

```sh
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  https://terrapod.example.com/api/terrapod/v1/admin/package-cache/warm \
  -d '{"packages":[{"ecosystem":"pypi","name":"requests"},
                   {"ecosystem":"npm","name":"left-pad","version":"1.3.0"}]}'
# → 202 {"data": {"id": "warm-9f2c...", "links": {"self": "…/admin/warm-jobs/warm-9f2c…"}}}
```

Submission returns a job id rather than a result: a real dependency closure
outlives an HTTP request, and a call that times out halfway leaves you unsure
what landed. Poll the job for progress and **per-item outcomes** — being told
"failed" across twenty packages is not something you can act on, and finding the
specific gaps is the entire point.

Omit `version` to warm the newest upstream offers. For PyPI, **every file for that
version is cached**, not one wheel: which wheel pip selects depends on the
interpreter and platform doing the installing. For npm the **packument is cached
alongside the tarball** — a sealed node cannot serve an install without it, since
the dependency ranges live there and nowhere else.

Re-running is safe and is the intended way to recover from a transient upstream
failure: anything already cached is skipped and only the rest is retried.

**Warming a sealed node is refused with a 409** naming `cache_only`, rather than
reporting zero successes — which would read as a set of missing packages rather
than a configuration that forbids fetching at all.

Container images warm the same way through `POST /api/terrapod/v1/admin/oci/warm`
with `{"images": ["quay.io/ansible/awx-ee:24.6.1"]}`, pulling the manifest and
every blob it references.


## Engine gating

These proxies exist to serve Pulumi programs and Ansible collections. PyPI serves
both, npm serves Pulumi only, so npm goes away with
`api.config.engines.pulumi.enabled: false` and PyPI only once *both* engines are
off. Cached artifacts are never deleted by turning an engine off. See
[engine gating](#engine-gating).

The engine switches sit **above** the per-capability flags in `registry`. A
capability serves only when its own flag is on *and* an engine that needs it is
enabled, so `registry.oci.enabled: true` does not bring the registry back once
Ansible is off. That is deliberate: switching an engine off should be one
decision, not a hunt for every capability that belongs to it.

| Capability | Needs |
|---|---|
| Container registry (`/v2/`) | `engines.ansible` |
| PyPI proxy | `engines.ansible` **or** `engines.pulumi` |
| npm proxy | `engines.pulumi` |

Terraform and OpenTofu's own caches — the provider network mirror, the engine
binary cache, the module registry — are not gateable and are unaffected by any of
this.
