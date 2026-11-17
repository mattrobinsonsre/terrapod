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

They also share **one cache**, with one row per artifact whatever ecosystem it
came from. To see what is in it, total it per ecosystem, or evict an artifact —
one, or in bulk by ecosystem and age — see
[Caches](caches.md#the-package-cache).

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

### What Terrapod checks before it caches anything

Passing the digest through is necessary and was never sufficient, because
**some clients check nothing**: `pip install` verifies no hash at all without
`--require-hashes` or a hash-pinned lock, which is the commonest case in the
commonest ecosystem. So on a miss Terrapod also verifies the bytes against
upstream's own published digest *before* storing them, and refuses to cache a
mismatch — neither the object nor the row is written, because a mismatch is
either a corrupted transfer or a substituted artifact and there is no telling
which from here.

Every cached row records **how it was admitted**, so the ledger can be read
rather than assumed:

| Recorded as | Means |
|---|---|
| `upstream-digest` | verified against a digest its publisher published |
| `none-published` | that upstream publishes no per-file digest, so there was nothing to check — a statement about the ecosystem, not about the artifact |

A row carrying neither was admitted before any of this existed; it is treated as
a miss, re-fetched once, verified under the current rules and rewritten.

**PyPI, npm, Ansible Galaxy and NuGet always publish one**, so an artifact
arriving from them *without* a digest is refused rather than cached unverified —
an absent digest there means the index was not the one we expect. Two ecosystems
publish nothing beside the artifact and are recorded `none-published`:

- **Go modules.** The proxy protocol serves no digest (`.ziphash` and
  `.zip.sha256` are both 404). For every module your program has **already
  pinned**, the verification happens elsewhere: against the Go **checksum
  database**, which Terrapod mirrors under `/sumdb/<host>/` so a run checks every
  module hash against the public transparency log. On by default from 2.0.

  Before 2.0 a run set `GOSUMDB=off`, because the database was a third upstream a
  sealed deployment could not reach. Mirroring it through this cache removed that
  constraint — and the reason given for leaving it off did not survive a
  measurement. It said the consumer verifies with a stronger control: the hashes
  in its committed `go.sum`, pinned by the program's author rather than asserted
  by whichever proxy answered. **True of a module already pinned, and false of one
  absent from the file.** Measured against go1.27.3 with a cold module cache:
  `go mod download` with `GOSUMDB=off` and no `go.sum` entry exits 0 and *writes*
  the hash of whatever the proxy served — it does not error, and nothing
  independent checks it. A run executes exactly that command against this cache,
  so an incomplete `go.sum` recorded our bytes as authoritative.

  **A sealed deployment must warm the checksum database before sealing.** A miss
  cannot be filled while sealed, so the dependency install fails rather than
  degrading. To run without it, set
  `api.config.registry.package_cache.go.sumdb_upstream` empty — which restores the
  pre-2.0 behaviour, including the gap above.
- **Pulumi plugins** published by Pulumi. `get.pulumi.com` publishes nothing
  beside a tarball (`.sha256` and `SHA256SUMS` are both 403). **Terrapod's own
  bridged plugin is the exception and *is* verified** — see
  [Pulumi plugins](#pulumi-plugins).

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
| `api.config.registry.package_cache.pulumi.first_party_url` | `https://github.com/mattrobinsonsre/terrapod/releases/download` | Where **Terrapod's own** bridged Pulumi provider plugin is published. Separate from `upstream` deliberately: pointing that one here would send every other plugin to a host which does not have it. **The `SHA256SUMS` manifest is fetched from here too**, so a mirror that carries it gets verified plugins. One holding only the tarballs still works: the absence is logged and the row records `none-published`, rather than refusing a plugin under configuration you already have. |
| `api.config.registry.package_cache.go.upstream` | `https://proxy.golang.org` | As above. |
| `api.config.registry.package_cache.nuget.upstream` | `https://api.nuget.org/v3-flatcontainer` | As above. |
| `api.config.artifact_retention.package_cache_retention_days` | `30` | Days since **last access** before an artifact is eligible for cleanup. Skipped entirely on a sealed node, where an evicted artifact cannot be re-fetched — note the [manual eviction routes](caches.md#sealed-deployments) are not, so on a sealed node they are a deletion. An **operator-uploaded** artifact is never eligible, on a sealed node or an unsealed one, because it exists upstream nowhere. |
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
  -d '{"packages":[{"ecosystem":"pypi","name":"requests","version":"2.32.3"},
                   {"ecosystem":"galaxy","name":"community.general","version":"9.2.0"},
                   {"ecosystem":"npm","name":"left-pad","version":"1.3.0"}]}'
# → 202 {"data": {"id": "warm-9f2c...", "links": {"self": "…/admin/warm-jobs/warm-9f2c…"}}}
```

**All six ecosystems** are warmable — `galaxy`, `pypi`, `npm`, `go`, `nuget` and
`pulumi` — each through its own pull-through adapter, so a warmed artifact is
byte-identical to one a run would have pulled and is held under the same key.

Submission returns a job id rather than a result: a real dependency closure
outlives an HTTP request, and a call that times out halfway leaves you unsure
what landed. Poll the job for progress and **per-item outcomes** — being told
"failed" across twenty packages is not something you can act on, and finding the
specific gaps is the entire point.

**Name a version wherever you know one.** A warm named at a version can
afterwards be checked against the lockfile your runs pin; "the newest" can be
checked against nothing. Omitting it stays supported as a convenience where the
registry publishes a meaningful newest, and is refused for `pulumi`, whose
download protocol has no index to resolve a bare name against.

Each ecosystem's warm also caches the index documents a sealed install resolves
through — galaxy's version list, npm's packument, go's `@v/list`, nuget's
versions document — because the artifact alone produces a cache that looks full
and cannot answer. For PyPI, **every file for that version is cached**, not one
wheel: which wheel pip selects depends on the interpreter and platform doing the
installing.

A submission may name a `source`: a label declared in this ecosystem's
`mirrors`, naming somewhere nearer than the public registry to warm *from*.
[Caches → Warming](caches.md#warming--filling-a-cache-before-anything-asks) has
the whole account, including what each ecosystem caches beyond the artifact and
how to read a job's per-item outcomes.

Re-running is safe and is the intended way to recover from a transient upstream
failure: anything already cached is skipped and only the rest is retried.

**Warming a sealed node is refused with a 409** naming `cache_only`, rather than
reporting zero successes — which would read as a set of missing packages rather
than a configuration that forbids fetching at all.

Container images warm the same way through `POST /api/v1/admin/oci/warm`
with `{"images": ["quay.io/ansible/awx-ee:24.6.1"]}`, pulling the manifest and
every blob it references.

### Seed-then-seal

Warming and sealing are one procedure rather than two features, and the whole
sequence is: run or submit the warms you expect to need while egress exists,
check the per-item outcomes, then set `registry.cache_only: true`. From that
point the deployment serves what it holds and reaches nothing.

The warm is the *seed*; `cache_only` is the *seal*. A warm submitted after the
seal is refused with a 409 naming the setting, which is why the order matters and
why the per-item outcomes are worth reading before you take the second step
rather than after.

## Restricting WHAT is served, not just where it comes from

`registry.provider_cache.upstream_registries` restricts which **hosts** Terrapod
reaches. Until now nothing restricted **what**: a workspace naming any package got
it faithfully fetched, verified against the publisher's own digest, and served —
verification working perfectly while an artifact signed by its own author executes.
Verification answers *"is this what the publisher published"*. It has no opinion on
whether this deployment wanted that publisher's package at all, and no amount of
scanning closes that gap.

`registry.content_allowlist` is that second control, and it is checked **before the
fetch** at the one pull-through point all six package caches share — an artifact that
has already been downloaded has already been reached for.

```yaml
api:
  config:
    registry:
      content_allowlist:
        mode: observe            # off (default) | observe | enforce
        packages:                # globs applied to every cache
          - "@acme/*"
        ecosystems:              # and per-cache, unioned with the above
          go:
            - "github.com/acme/*"
```

### The three modes, and why it ships `off`

| mode | what it does |
|---|---|
| `off` | the default. No allowlist, and **no per-fetch log line** — a control nobody configured must not fill the log, which is how a feature gets switched off wholesale before it is ever used |
| `observe` | records what it **would** refuse and fetches it anyway |
| `enforce` | refuses anything unmatched, fail-closed |

**Run `observe` first.** Nobody can hand-write an allowlist for an estate they do not
yet understand: deny-all is unusable on day one and allow-all makes the claim false.
`observe` is what tells you what your own estate actually pulls, so promoting that set
to `packages` is one review rather than an archaeology project. `enforce` then fails
closed against a set you have already approved, rather than being a cliff you walk off
on upgrade.

**An empty list under `enforce` refuses everything, deliberately.** That is loud rather
than silent — every fetch fails at once and the message names the setting and the way
back. A mode that quietly permitted everything when the list was empty would make the
whole posture a false claim, which is worse than an outage.

Patterns are globs rather than regexes (so a mis-escaped `.` cannot silently match more
than it reads as) and are matched case-insensitively, because every cache this guards
either addresses packages case-insensitively or normalises before the check. A blank
pattern matches **nothing** rather than everything — a stray blank line in a values file
must not read as a wildcard.

A refusal subclasses `PermissionError`, so a `except OSError` written for the network
cannot swallow it and leave a policy decision looking like a flaky upstream.

## Uploading a vetted artifact

Seed-then-seal pulls from a registry and trusts what it published. Some
organisations have the opposite rule: **nothing runs here that we have not vetted
ourselves.** For those, an operator uploads the artifact — having unpacked it,
reviewed it and run it through their own approval process — and the sealed
deployment serves that and nothing else.

This is a different trust model rather than a weaker one. Verification at
admission answers *"did this come from the publisher it claims?"*. An operator
who has already approved an artifact is asserting something else: *"this exact
byte sequence is the one we approved"*. That is a **digest**, not a signature, and
their process is the authority — which is why no publisher signature is required
and why requiring one would defeat the feature. The likeliest thing uploaded here
is something built, patched or re-packaged in-house, which no upstream key covers.

What stands in for the signature is **provenance**, and in an audit it is the
stronger claim: a named person in your own organisation put these bytes here, on
this date, against your own approval record.

### One endpoint per cache

Each ecosystem takes its own coordinates, because each already computes its cache
key from exactly those fields — an upload is the same cache key arrived at from
the other direction. Every one is `multipart/form-data` with the bytes in a
`file` part, so a browser form drives it unchanged.

| Route (all `POST`) | Coordinates |
|---|---|
| `/api/v1/admin/package-cache/pypi/artifacts` | `project`, `filename` |
| `/api/v1/admin/package-cache/npm/artifacts` | `name`, `version` |
| `/api/v1/admin/package-cache/nuget/artifacts` | `package_id`, `version` |
| `/api/v1/admin/package-cache/go/artifacts` | `module`, `version`, `suffix` (`.info`, `.mod` or `.zip`) |
| `/api/v1/admin/package-cache/galaxy/artifacts` | `namespace`, `name`, `version` |
| `/api/v1/admin/package-cache/pulumi/artifacts` | `kind`, `name`, `version`, `os`, `arch` |

Every route also takes `reference` — free text, your own ticket or approval
record — and `replace`, which is discussed below.

```sh
curl -X POST -H "$AUTH" \
  https://terrapod.example.com/api/v1/admin/package-cache/pypi/artifacts \
  -F project=requests \
  -F filename=requests-2.32.3-py3-none-any.whl \
  -F reference=CHG-4471 \
  -F file=@requests-2.32.3-py3-none-any.whl
```

**Uploading requires the `admin` platform role.** Who may put bytes into what a
deployment serves is a deployment-wide trust assertion, not a per-workspace one.

**This works while sealed**, which is the point: every other write to these
caches is an upstream fetch, so without it a `cache_only` deployment would have
no way to get an artifact in at all.

**The coordinates are never checked against an upstream index.** A patched
version, an internal package, a rebuilt binary — none of them exist upstream, and
those are the cases this exists for. They are checked only against the
ecosystem's own naming rules, because they become a cache key and a storage path.

### The record says uploaded by

An uploaded artifact is never indistinguishable from a fetched one.
`GET /api/v1/admin/package-cache` reports, per artifact:

| Attribute | For an upload |
|---|---|
| `attestation` | `operator-upload` — against `upstream-digest` for a verified fetch, or `none-published` where upstream publishes no digest |
| `uploaded-by` | the admin who uploaded it |
| `upload-reference` | whatever `reference` was given, or null |
| `digest` | the digest computed **on receipt**; for a fetch this is upstream's own |
| `cached-at` | when it was uploaded |

### Replacing one is deliberate

An upload at coordinates already held is **refused with 409**, naming the digest
that is there and how it was admitted. What you would be overwriting may be
exactly what a run already depends on, so it is never silent.

Pass `replace=true` to go ahead. The displaced digest is written to the row as
`replaced-digest`, with `replaced-at`, so the change is on the record rather than
only in the audit log. On a sealed node the response says plainly that the bytes
you replaced cannot be fetched back.

Re-uploading the **identical** artifact is not a replacement and is not refused:
you are re-asserting what is already recorded. It also re-writes the object, which
is the one repair available for an uploaded artifact whose bytes have gone from
storage — nothing can re-fetch it.

### Checking an upload later

A fetched artifact can be re-verified at any time, because the publisher's
material is still upstream. An uploaded one cannot be re-verified against
anything except the digest recorded when it arrived — so that digest is the whole
of its integrity story, and this is the part that must not become "we never look
at it again".

Two checks, because a cache read is a redirect to object storage and the API
never sees the bytes a client is served:

* **on every serve**, an uploaded artifact's stored object is compared against
  the size on its row. A mismatch refuses the request rather than reporting a
  cache miss — a miss would be filled from upstream, silently replacing vetted
  content with unvetted under the coordinates you curated;
* **on demand**, `POST /api/v1/admin/package-cache/{id}/verify` re-reads the
  object and re-hashes it. `ok` is `true`, `false`, or `null` where there was no
  recorded digest to verify against. Nothing is repaired or evicted either way:
  what to do about a mismatch is yours to decide.

### From the admin UI

Everything above is also on the **Caches** admin page, under the package-cache
listing it fills: pick the cache, pick whether you are uploading the artifact's
bytes or an index document, fill that cache's coordinates, choose the file.

Three things the page does that the API does not:

- **It shows where each cached artifact came from.** An `Origin` column reads
  *Verified* where the bytes were checked against the digest the registry
  published, *No digest published* where that ecosystem publishes nothing to
  check against, *Uploaded* for an operator upload (with who uploaded it), and
  *Not recorded* for a row written before this was tracked. The last of those is
  not a finding: such a row is re-fetched and re-verified on the next request
  rather than served.
- **It warns before evicting an upload.** An uploaded artifact exists in no
  upstream registry, so removing one destroys the only copy *however unsealed the
  node is* — a different warning from the sealed one, and the one that is easy to
  miss, because the node being able to re-fetch everything else makes the
  eviction look recoverable.
- **The index-document choice is offered only where there is one.** For PyPI and
  Pulumi the control is disabled and says so, rather than being absent and
  leaving you to wonder.

The upload goes through the BFF's `/api/*` proxy, which forwards the request body
unbuffered — the same path a configuration-version tarball already takes, so a
large artifact is streamed rather than held in the web pod's memory.

### Uploading an index document

An artifact upload supplies **bytes**, and for three of the six caches that is not
enough to make the artifact reachable. A sealed node answers "what versions exist"
from a cached index document and nowhere else, so a vetted artifact with no document
beside it is present, recorded, and unresolvable — the install is told the package is
not cached. npm is the clearest case: the dependency ranges live in the packument and
in no other file.

So each of those three takes a document too, at the same admin gate, with the same
provenance and the same deliberate-replacement rule:

| cache | route | form fields |
|---|---|---|
| npm | `POST /api/v1/admin/package-cache/npm/index` | `name`, `file` (the packument) |
| Galaxy | `POST /api/v1/admin/package-cache/galaxy/index` | `namespace`, `name`, `document` (`collection` or `versions`), `file` |
| NuGet | `POST /api/v1/admin/package-cache/nuget/index` | `package_id`, `file` |
| Go | `POST /api/v1/admin/package-cache/go/index` | `module`, `document` (`list` or `latest`), `file` |

Galaxy needs **two** — a sealed node reads the collection record and the version list
from separate documents, so one without the other leaves half the path still
answering 404.

**Go is the one you may not need.** Its documents are required only to *discover* a
version: `go get module` with no version, or `go list -m -versions`. A pinned
`go mod download` asks for `.info`, `.mod` and `.zip` directly and never consults
them, so a deployment that pins everything can skip it.

A JSON document that does not parse is **refused at upload** rather than stored,
because a sealed node parses it on every read — one that does not parse would fail
every subsequent resolution rather than this one request. Go's `@v/list` is a
newline-separated list rather than JSON and is correctly exempt.

**PyPI and Pulumi need nothing extra.** PyPI builds its sealed simple index from the
artifacts it holds, so an upload is advertised the moment it exists; Pulumi plugins
are addressed by filename with no index at all. Neither has a route here, and that
absence is deliberate rather than unfinished.

**An uploaded artifact is not re-fetchable from anywhere.** For a sealed
deployment that makes storage-layer replication of this cache's objects a
requirement rather than a choice: a failover onto a follower whose blob store was
not replicated leaves it holding a complete record of what should be there and
unable to serve any of it, and the only recovery is re-uploading the whole
curated set.

The same fact is why **the age-based retention sweep never reaps an upload**. That
is not covered by the sealed-mode skip, and the case it protects is the ordinary
one: `seed-then-seal` has you upload while the node is still unsealed, so the
uploads sit for as long as that preparation takes on a node where the sweep does
run. A deployment that stays unsealed and uses uploads for its internal packages
would otherwise lose them to a timer. Deliberate removal is unaffected — the
admin eviction route still takes an upload by id, and says it is the only copy.

**An upload is never gated on a scan of ours.** Terrapod does not scan
package-cache artifacts at all today — not uploaded ones and not fetched ones —
and the settled position is that if it ever does, a finding on an upload is
reported rather than enforced: gating on our own scan would second-guess the
attestor we have just said we trust.


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

### What is cached, and for how long

The split is the same one the Go and NuGet proxies make, for the same reason:
a document is bounded if its content can change upstream, and not otherwise.

| Document | Mutable? | Served from cache for |
|---|---|---|
| `collections/{ns}/{name}/` and `.../versions/` | yes — a new version changes both | **one hour**, then re-read upstream |
| `.../versions/{version}/` | no — a published version is immutable | indefinitely |
| the collection tarball | no | indefinitely |

So a version published upstream within the hour may not be offered yet by a
deployment that has already resolved that collection — `ansible-galaxy` reads
the listing even for an exact pin, so an install of a brand-new version can
resolve on the hour rather than at once. A collection the deployment has never
seen is unaffected: there is nothing cached to be stale.

An unsealed node reads these back before it asks upstream. Earlier releases did
not: they fetched every document on every request and stored a copy only a
sealed node would ever read, so resolving five pinned collections through a warm
proxy still paid every upstream round trip in series and took tens of seconds.

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

A configure installs them through the PyPI proxy above, from two places:

1. **every installed collection's own** `requirements.txt`, found under
   `ansible_collections/<namespace>/<name>/` once the collection is on disk.
   These are installed **together, in one `pip install`**, so pip's resolver sees
   every collection's pins at once and either finds a set that satisfies all of
   them or says which cannot be satisfied together. One file at a time, a later
   collection could upgrade a shared dependency past a constraint an earlier one
   needed — and pip reports that and exits 0, so the configure carried on with
   controller imports already broken.
2. **yours**, a `requirements.txt` beside the `requirements.yml` you already
   declare, for a package no collection names — a client a `uri` task needs, or a
   filter plugin of your own. Installed **after** step 1, as a separate step, so
   a later install still wins a conflicting pin and your file remains the one
   that decides. That is what makes it usable for pinning around a collection's
   bad constraint; folded into step 1 it would be one more equal requirement, and
   a pin that deliberately contradicts a collection would fail to resolve rather
   than win.

The order within step 1 is sorted, so a conflict cannot vary between runs.

**The result is checked, and an inconsistent one fails the configure.** `pip
install` prints a dependency conflict and exits 0, so `pip check` runs afterwards
and a non-zero exit fails the phase, naming the conflict in the run log. Fix it
by pinning the package in your own `requirements.txt`, which is applied last.

`pip check` also runs **before** anything is installed. If the execution
environment's packages were already inconsistent, that is not something the
configure did, so it is reported as a warning and the run continues — a
configure that has been working is not failed in order to report a conflict the
image shipped with.

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

**For a Pulumi-published plugin no digest is recorded**, because
`get.pulumi.com` publishes none alongside the tarball — `.sha256` and
`SHA256SUMS` are both 403. The row says `none-published` rather than being left
to look verified. That is weaker than the PyPI, npm, Galaxy and NuGet proxies,
where Terrapod verifies our bytes against a published digest before caching
them.

**Terrapod's own bridged plugin is verified, best effort.** We publish it, so its
release carries a `SHA256SUMS` manifest beside the tarballs: on a miss Terrapod
fetches that manifest from the same release, finds the line for the exact asset it is
about to cache, and verifies the bytes against it. The row records `upstream-digest`.

The split between what refuses and what does not is the part worth understanding:

| what happens | result |
|---|---|
| the manifest lists this asset and the bytes **match** | cached, recorded `upstream-digest` |
| the manifest lists this asset and the bytes **do not match** | **refused** — nothing stored, nothing recorded |
| the manifest names this asset against something that is not a sha256 | **refused** — it is making a claim we cannot read |
| the manifest is absent, unreachable, empty, or does not list this asset | **cached**, recorded `none-published`, and logged at `WARNING` |

The last row is deliberate and was the other way round until 2026-11-16. Requiring
the manifest reads as the stricter choice, and it breaks a supported configuration:
an operator whose `first_party_url` points at an internal mirror holding only the
tarballs would have those plugins refused on upgrade, under configuration they
already had, with no warning. The integrity check that actually matters — do these
bytes match what was published — is unaffected by admitting an absence, so the
absence is logged rather than fatal.

Two consequences worth knowing before you point `first_party_url` somewhere else:

- **An internal mirror of our release should carry the `SHA256SUMS` manifest too**,
  not just the tarballs, if you want those plugins verified. It is fetched from the
  same base, under the same `v{version}/` path segment. A mirror holding only the
  tarballs still serves them; they are recorded `none-published`.
- **`source` on a warm request is unaffected.** A warm from a named mirror
  replaces the whole upstream flat, with no version path segment, so there is no
  manifest to derive and that path is admitted and recorded `none-published`
  exactly as it was before.


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

**Nothing here is verified at fetch time, deliberately**, and a cached row says
`none-published`. For a module your program has **already pinned**, your
toolchain verifies it against the Go **checksum database**, which Terrapod
mirrors for you under `/sumdb/<host>/` — so the hash is checked against the
public transparency log rather than taken from whichever proxy answered. The
proxy protocol publishes no digest beside these files, so this is where the
verification lives.

This is on by default from 2.0. Before that a run set `GOSUMDB=off`, because the
database was a third upstream a sealed deployment could not reach; mirroring it
through this cache is what removed that constraint.

**A complete `go.sum` is still worth having, and it is no longer the only thing
between you and an unverified module.** With `GOSUMDB=off`, a module missing from
`go.sum` is not covered at all: `go mod download` writes the hash of whatever the
proxy served and exits 0 (measured, go1.27.3, cold cache). With the database on,
that module is checked against the log. See
[What Terrapod checks before it caches anything](#what-terrapod-checks-before-it-caches-anything).

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
**That includes that ecosystem's [upload](#uploading-a-vetted-artifact) route**,
which goes with its own flag rather than with the cache's: there is no point
offering somewhere to put an artifact this deployment will not serve. The shared
routes — the listing, the totals, the evictions and verify — are about the cache
rather than about an ecosystem, so they stay.
A surface that refuses every request is still a surface: it sits in the schema,
carries its dependencies, and reads to anyone auditing the deployment as
something you do.

**Never destructive.** Cached artifacts stay exactly where they are and come
back untouched when you turn the proxy on again.

Terraform and OpenTofu's own caches — the provider network mirror, the engine
binary cache, the module registry — have no such switch. They are what Terrapod
is, not an optional engine's supporting cast.
