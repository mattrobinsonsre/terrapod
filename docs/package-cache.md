# Language package proxies

Terrapod proxies the registries a run resolves its language dependencies from,
so it can resolve its whole closure without reaching the internet. Point a
runner at them and `pip install`, `npm install`, `go mod download`,
`dotnet restore`, `ansible-galaxy install` and a Pulumi plugin download all
work inside a sealed network.

| Ecosystem | Serves | Point a client at it with |
|---|---|---|
| [PyPI](#using-it) | Pulumi Python programs, Ansible collections' Python dependencies, `ansible-builder` | `PIP_INDEX_URL` |
| [npm](#using-it) | Pulumi TypeScript and JavaScript programs | `NPM_CONFIG_REGISTRY` |
| [Ansible Galaxy](#ansible-galaxy) | collections — pull-through, private publishing, signatures | a `galaxy_server` entry |
| [Pulumi plugins](#pulumi-plugins) | the resource plugin `pulumi up` fetches per provider | `PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES` |
| [Go modules](#go-modules) | Pulumi programs written in Go | `GOPROXY` |
| [NuGet](#nuget) | Pulumi programs written in C# | a `nuget.config` `packageSource` |

```yaml
# The runner's environment
PIP_INDEX_URL: https://terrapod.example.com/api/v1/package-cache/pypi/simple
NPM_CONFIG_REGISTRY: https://terrapod.example.com/api/v1/package-cache/npm/
```

Each has its own section below; they share one substrate, so the integrity,
air-gap and retention behaviour described here applies to all six.

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

Every proxy requires authentication, including for reads. Any Terrapod
credential works.

**pip** sends Basic auth, so the credential goes in the index URL or `.netrc`:

```sh
pip install --index-url "https://any:$TERRAPOD_TOKEN@terrapod.example.com/api/v1/package-cache/pypi/simple" flask
```

**npm** sends a bearer token, configured in `.npmrc`:

```ini
registry=https://terrapod.example.com/api/v1/package-cache/npm/
//terrapod.example.com/api/v1/package-cache/npm/:_authToken=${TERRAPOD_TOKEN}
```

A runner's own short-lived token works for all of them, so a run authenticates
as itself rather than carrying a shared credential.

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
| `api.config.registry.package_cache.enabled` | `true` | Serve the proxies. All six at once. |
| `...package_cache.{pypi,npm,galaxy,pulumi,go,nuget}.enabled` | `true` | Per ecosystem. Off means the routes are **never registered** — see [Turning a proxy off](#turning-a-proxy-off). Cached artifacts stay and reappear if re-enabled. |
| `api.config.registry.package_cache.pypi.upstream` | `https://pypi.org` | Where to pull through to. Point it at an internal mirror to cache that instead. |
| `api.config.registry.package_cache.npm.upstream` | `https://registry.npmjs.org` | As above. |
| `api.config.registry.package_cache.galaxy.upstream` | `https://galaxy.ansible.com` | As above. |
| `api.config.registry.package_cache.pulumi.upstream` | `https://get.pulumi.com/releases/plugins` | As above. Where **Pulumi's** plugins come from. |
| `api.config.registry.package_cache.pulumi.first_party_url` | `https://github.com/mattrobinsonsre/terrapod/releases/download` | Where **Terrapod's own** bridged Pulumi provider plugin is published. Separate from `upstream` deliberately: pointing that one here would send every other plugin to a host which does not have it. |
| `api.config.registry.package_cache.go.upstream` | `https://proxy.golang.org` | As above. |
| `api.config.registry.package_cache.nuget.upstream` | `https://api.nuget.org/v3-flatcontainer` | As above. |
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

## Warming ahead of a seal

Warming by *running the thing you expect to need* is a rehearsal: you find out
whether you guessed the set right only after sealing, which is the expensive
moment to discover a gap.

```sh
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  https://terrapod.example.com/api/v1/admin/package-cache/warm \
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

Container images warm the same way through `POST /api/v1/admin/oci/warm`
with `{"images": ["quay.io/ansible/awx-ee:24.6.1"]}`, pulling the manifest and
every blob it references.


## Ansible Galaxy

`ansible-galaxy` resolves collections from `galaxy.ansible.com` at install time,
so an estate that cannot reach it cannot install a collection at all. Point the
client at Terrapod with a `galaxy_server` entry:

```ini
# ansible.cfg
[galaxy]
server_list = terrapod

[galaxy_server.terrapod]
url = https://terrapod.example.com/api/v1/package-cache/galaxy/
token = <a Terrapod API token>
```

`ansible-galaxy collection install community.general` then resolves and downloads
entirely through Terrapod, and installs again with no route to the internet once
the collection is cached.

### Collections and roles are two mechanisms

**Collections** are usually external, so they come through the pull-through proxy
above — that is the optimised path, and it is what lets a runner install one with
no route to `galaxy.ansible.com`. A collection published to this deployment's own
registry is answered from there and never proxied.

**Roles** are usually in-house, so they come from git. An operator declares the
credential once as a `git_http_auth` or `git_ssh_auth` workspace variable and the
runner materialises it into git's own configuration before anything fetches, so a
`roles:` entry naming a git source clones with no extra setup. Both are installed
from the same `requirements.yml`, with `ansible-galaxy role install` and
`ansible-galaxy collection install` run in turn.

Both kinds work either way, with one exception: **a Galaxy-hosted role** (a
`roles:` entry naming `namespace.name` with no `src`) uses the v1 roles API, which
Terrapod does not serve. It reaches upstream directly where there is a route out
and fails where there is not, so a sealed deployment should use a git source or
vendor the role into a collection.

### A collection's own Python dependencies

`ansible-galaxy` installs a collection's **content** and not the Python packages
it needs. `amazon.aws` ships a `requirements.txt` naming `boto3` and `botocore`,
and installing the collection does nothing about those — so it installs cleanly
and then fails at the first task, or at plugin load for a dynamic inventory, with
an import error naming the SDK.

A configure installs them through the PyPI proxy above, from two places, in this
order:

1. **each installed collection's own** `requirements.txt`, found under
   `ansible_collections/<namespace>/<name>/` once the collection is on disk;
2. **yours**, a `requirements.txt` beside the `requirements.yml` you already
   declare, for a package no collection names — a client a `uri` task needs, or a
   filter plugin of your own.

A later install wins a conflicting pin, so your file is the one that decides. Two
collections pinning the same package differently is yours to resolve, and the
order within step 1 is sorted so it will not vary between runs.

The install runs **inside the execution environment**, because that is the
interpreter which has to import the package, and `--user` into its home on the
shared volume, because its root filesystem is read-only. The proxy credential goes
in a `.netrc` there rather than into the index URL: pip prints its index, and a
run's log is streamed to the API and the UI.

**A failed install fails the configure.** A missing SDK fails the run anyway — at
the first task, against real hosts, half way through — so refusing it up front is
the better outcome.

**An execution environment with no pip is skipped, with a warning naming the files
it did not install.** pip is not part of the contract an image must satisfy, so an
operator-supplied image that conforms today may legitimately not carry one;
Terrapod's own images do. If you [supply your own](runners.md#choosing-an-execution-environment)
and want this, add pip to it or install the dependencies into the image directly.

### Declaring collections on the workspace

A workspace can declare collections itself, through `ansible-collections` on the
API, `terrapod_workspace`'s `ansible_collections` attribute, bulk update, or an
autodiscovery rule template. They are installed **after** the configuration's own
requirements file and with `--force`, so a version declared on the workspace wins
over one committed in the repository and every other committed collection stands.

That exists because a collection is a supply-chain dependency: declared on the
platform it is visible in a plan, governed by workspace RBAC, in the audit log,
and movable across a fleet in one operation.

The endpoints served are the ones the client actually calls, catalogued in
[the Galaxy CLI surface](galaxy-cli-surface.md) — captured from a real client
rather than read from documentation, which is how four of that document's
statements came to contradict the obvious reading of the docs.

### Publishing a private collection

`ansible-galaxy collection publish acme-widgets-1.0.0.tar.gz` publishes to the
same server entry. Terrapod reads the coordinates, dependencies and digest out
of the archive's own `MANIFEST.json` — the client sends nothing else — and the
collection then installs exactly like a public one, because the version detail
it serves is the same shape.

A published version is **immutable**: republishing the same version is refused
rather than replacing it, since a client that has already resolved it and cached
its digest would otherwise receive different bytes under the same name.

**Who may publish.** A namespace nobody has published to yet is open to any
authenticated principal, and whoever creates it becomes its owner — the same rule
the module and provider registries follow. Once a collection exists, publishing a
further version into it requires **`registry:write` on that collection**, resolved
from its owner and labels exactly as for a registry module. So a second publisher
cannot add versions to someone else's namespace, which matters more here than
elsewhere because the namespace is declared inside the archive rather than given
in the URL: reading it from `MANIFEST.json` stops a caller *claiming* a namespace
they do not own, but not building a tarball that declares one.

**Runner tokens cannot publish or sign**, whatever capabilities their workspace
would otherwise resolve to. A run's own short-lived token exists to download
dependencies, not to add artifacts to the registry.

Published collections are **not** cache entries and are never evicted by the
retention sweep. A cached artifact is a copy of something upstream still has; a
published one is the only copy there is.

### Signing a published collection

`collection publish` sends only the tarball, so there is nowhere in that
protocol to put a signature. Attach one afterwards:

```sh
curl -X PUT \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  --data-binary @manifest.sig \
  "$TERRAPOD/api/v1/package-cache/galaxy/v3/collections/acme/widgets/versions/1.0.0/signature"
```

Signing requires **`registry:write` on the collection**, as publishing does, and
the collection must already exist — there is no create-by-signing path, so an
unknown collection is a 404 rather than a permission answer.

The body is a detached OpenPGP signature over the collection's `MANIFEST.json`.
Terrapod verifies it against a **public key already registered** with the
platform (`/api/v1/gpg-keys`) and refuses a signature from an
unregistered key with 422, naming the key. The server never re-signs — the
publisher owns the signature, exactly as in the provider registry.

Once verified, the signature appears in the version detail, so
`ansible-galaxy collection install --keyring …` can check it.

**What is and is not rewritten.** Every URL the client follows —
`download_url`, `versions_url`, every `href` — points back at Terrapod, and
upstream's pagination cursors are dropped rather than forwarded. A collection's
own `repository` and `documentation` links are left exactly as published: the
client never fetches them, and rewriting them would simply be untrue.
`artifact.sha256` passes through untouched, because the client checking our
bytes against upstream's digest is the whole security model here.

**Sealed** (`registry.cache_only: true`), a cached collection still installs and
an uncached one fails with a message naming the setting, rather than quietly
reaching upstream. The version list is narrowed to versions actually held, so the
client is never offered one that would 404 after it has resolved.


## Pulumi plugins

`pulumi up` downloads a resource plugin per provider on first use, from
`get.pulumi.com`. Point the CLI at Terrapod instead:

```sh
export PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES=".*=https://x:$TERRAPOD_TOKEN@terrapod.example.com/api/v1/package-cache/pulumi"
```

Credentials go in the URL because the CLI sends no `Authorization` header of its
own; userinfo becomes Basic auth, and the username is ignored.

**Do not anchor the pattern.** `^random$` never matches, and a pattern that
matches nothing makes the CLI fall back to `get.pulumi.com` **silently** — the
install succeeds, and the failure only appears once someone has no route out.
Use `.*`, or a bare unanchored plugin name. The measured behaviour is in
[the Pulumi CLI surface](pulumi-cli-surface.md).

The whole protocol is a single request for one well-known filename, so unlike
the other proxies there is no version list here and nothing that needs a TTL —
a plugin at a version is immutable.

**Pin your plugin versions.** An unpinned program first resolves what "latest"
means, and that resolution does not go through the download URL, so it still
needs upstream. Pinning is good practice in a restricted estate regardless.

**No digest is recorded**, because upstream publishes none alongside the
tarball. That is weaker than the PyPI, npm and Galaxy proxies, where the client
verifies our bytes against a published digest.


## Go modules

A Pulumi program written in Go resolves its dependencies from
`proxy.golang.org`. Point the toolchain at Terrapod instead:

```sh
export GOPROXY="https://x:$TERRAPOD_TOKEN@terrapod.example.com/api/v1/package-cache/go"
```

**This must be HTTPS.** The Go toolchain refuses to send credentials to a plain
HTTP URL — `refusing to pass credentials to insecure URL` — and neither
`GOINSECURE` nor `.netrc` lifts that. Every real deployment terminates TLS, so
this only bites a plain-HTTP development setup.

Five paths, and the split between them is the design: `@v/list` and `@latest`
say what exists and are **mutable**, so they are bounded; `@v/{version}.info`,
`.mod` and `.zip` are **immutable** and need no TTL at all, because a module
version's bytes cannot change — which is what Go's checksum database exists to
guarantee.

Uppercase in a module path is escaped — `github.com/BurntSushi/toml` is fetched
as `github.com/!burnt!sushi/toml` — and Terrapod passes that form through
unchanged, because upstream expects it too.

The toolchain **probes parent prefixes** while resolving, asking about
`example.com`, `example.com/a` and `example.com/a/b` in turn to find the module
root. Most of those are misses by design, so a miss is a plain 404 rather than
an error; anything else would break resolution for any module path with more
than one segment.

## NuGet

A Pulumi program written in C# resolves from `nuget.org`. Add Terrapod as a
package source:

```xml
<!-- nuget.config -->
<configuration>
  <packageSources>
    <clear/>
    <add key="terrapod"
         value="https://terrapod.example.com/api/v1/package-cache/nuget/index.json" />
  </packageSources>
  <packageSourceCredentials>
    <terrapod>
      <add key="Username" value="x" />
      <add key="ClearTextPassword" value="%TERRAPOD_TOKEN%" />
    </terrapod>
  </packageSourceCredentials>
</configuration>
```

The **service index** is built per request rather than stored, because it
advertises absolute URLs the client then follows verbatim. Those must carry this
deployment's external base and this proxy's path prefix — set `external_url` and
they are correct however many proxies sit in front. A stored index would pin
whatever host fetched it first and hand that to everyone afterwards.

Package ids are lowercased in every path, so `Newtonsoft.Json` and
`newtonsoft.json` are one cache entry. The versions document is **mutable** and
bounded; a `.nupkg` at a version is **immutable** and needs no TTL.

`dotnet restore` warns (NU1803) against an HTTP source. Production is HTTPS, so
this only appears in local testing.


## Turning a proxy off

Every proxy is on by default and each has its own switch. There is **no engine
on/off switch above them** — Terrapod offers every engine it can run, so these
flags are the only thing that decides whether a proxy serves.

| Proxy | Switch |
|---|---|
| all six at once | `api.config.registry.package_cache.enabled` |
| PyPI | `api.config.registry.package_cache.pypi.enabled` |
| npm | `api.config.registry.package_cache.npm.enabled` |
| Galaxy | `api.config.registry.package_cache.galaxy.enabled` |
| Pulumi plugins | `api.config.registry.package_cache.pulumi.enabled` |
| Go modules | `api.config.registry.package_cache.go.enabled` |
| NuGet | `api.config.registry.package_cache.nuget.enabled` |

Setting one to `false` means the routes are **never registered** — the proxy is
absent from the API and from the OpenAPI schema, not mounted and answering 404.
A surface that refuses every request is still a surface: it sits in the schema,
carries its dependencies, and reads to anyone auditing the deployment as
something you do.

**Never destructive.** Cached artifacts stay exactly where they are and come
back untouched when you turn the proxy on again.

Terraform and OpenTofu's own caches — the provider network mirror, the engine
binary cache, the module registry — have no such switch. They are what Terrapod
is, not an optional engine's supporting cast.
