# Caches — what is in them, and how to empty them

Terrapod keeps four pull-through caches so a run can resolve everything it needs
without reaching the internet: the CLI binary it executes, the providers it
initialises, the language packages its program or its collections depend on, and
the repository tarball it runs against.

**Evicting the wrong thing costs you a slow next run, not data.** Every artifact
in all four came from somewhere else and is re-fetched on the next miss — that is
what makes these caches rather than stores, and it is why an eviction needs no
ceremony. The one exception is a **sealed** deployment, where nothing can be
re-fetched; read [Sealed deployments](#sealed-deployments) before evicting
anything there.

**Filling one deliberately is [warming](#warming--filling-a-cache-before-anything-asks)**,
which is how a restricted-network deployment is prepared: you fill the caches
while upstream is still reachable, then seal the node.

---

## The four caches

| Cache | Filled by | Lives in | Inspect | Evict by hand |
|---|---|---|---|---|
| **CLI binaries** (`tofu`, `terraform`, `terragrunt`, `pulumi`, the language runtimes `node`, `go` and `dotnet` that a Pulumi program may need, and the platform tools `opa`, `trivy` and `checkov`) | a run resolving its engine version; a policy evaluation or a security scan resolving `opa`, `trivy` or `checkov` | object storage under `cache/binaries/` **and** the `cached_binaries` table | `GET /api/v1/admin/binary-cache` | `DELETE /api/v1/admin/binary-cache/{tool}/{version}` |
| **Provider binaries** | `terraform init` / `tofu init` through the [provider network mirror](registry.md#provider-caching-network-mirror) | object storage under `cache/providers/` **and** the `cached_provider_packages` table | `GET /api/v1/admin/provider-cache` | `DELETE /api/v1/admin/provider-cache/{hostname}/{namespace}/{type}/{version}` |
| **Package cache** — PyPI, npm, Go modules, NuGet, Ansible Galaxy, Pulumi plugins | a Pulumi program resolving its language SDK; a configure installing collections; `ansible-builder`. See [Package proxies](package-cache.md) | object storage under `cache/packages/` **and** the `cached_package_files` table — one table for all six ecosystems | [`GET /api/v1/admin/package-cache`](#the-package-cache) | one artifact, or in bulk by ecosystem and age |
| **VCS archive cache** | a poll cycle or a run fetching a configuration version from a VCS connection | **object storage only** — `vcs_archives/{connection}/{owner}/{repo}/{sha}-{paths_hash}.tar.gz`, and that key *is* the record | [`GET /api/v1/admin/vcs-archive-cache`](#the-vcs-archive-cache) | one archive by key, or in bulk by connection and age |

Every route above requires platform `admin`, and each is served on the canonical
`/api/v1` prefix and on the deprecated `/api/terrapod/v1` alias (see
[Deprecations](deprecations.md)).

The first two have had admin routes since they existed and are documented with
the registry they serve — see
[binary cache admin](registry.md#api-endpoints) and
[provider cache admin](registry.md#provider-cache-admin-endpoints). The other two
are covered below.

Each surface reaches the same routes: the API directly, `go-terrapod`, the
`/admin/binary-cache` page, and six MCP tools ([MCP](mcp.md)) — the two listings,
annotated read-only, and the four evictions, annotated **destructive** so an MCP
host asks before running one.

> This page used to say the evictions were deliberately *not* offered as tools,
> because an eviction's only result is a slower run later for somebody who did
> not ask. The cost is real and the four tool descriptions say so; what was wrong
> was treating it as a reason to withhold the tool. It is a property of the
> operation rather than of the caller, so it applies just as much to the admin
> page and to `curl`, and MCP is not an authorization boundary — the server
> carries the operator's own token and the API applies the same RBAC whichever
> door the call comes through. All the omission achieved was an agent that could
> see a cache holding 40 GiB and could not act on it.

### What is not in this list, and why

Three things get called caches and are not in the table, each for its own
reason. If you came here having heard one of these names, this is the answer
rather than a gap in the page.

- **`vcs_metadata_cache`.** The poller memoises a repository's head SHA, default
  branch and open pull requests so that fifty workspaces on one monorepo make
  one VCS API call between them instead of fifty. It is **in memory and lives
  for exactly one poll cycle** — holding it longer would stop the poller
  noticing new commits, which is the whole point of polling. So there is nothing
  to enumerate and nothing an eviction could mean: the next cycle starts with an
  empty one regardless.
- **The [container registry](oci-registry.md).** Its pull-through mirror is a
  cache, but it shares one content-addressed blob store with images you
  *pushed*, which exist nowhere else. A layer therefore lives as long as some
  manifest references it rather than as long as something is using it, and
  reclaiming space is a garbage-collection cycle rather than an eviction. It has
  its own admin surface and its own page.
- **Redis memoisations** — a resolved version string, a token's roles, a
  translated plan summary. Each is derived, short-lived and keyed under `tp:`;
  they hold no artifacts and cost nothing to lose.

---

## Automatic eviction comes first

All four caches are swept on a schedule by
[artifact retention](artifact-retention.md), so in steady state there is nothing
to do by hand. The routes below are for the cases a schedule cannot serve: a bad
artifact to remove now, or a bucket that has grown and a question about what is
in it.

| Cache | Retention key | Default | Keyed on |
|---|---|---|---|
| CLI binaries | `api.config.artifact_retention.binary_cache_retention_days` | 30 days | last access |
| Provider binaries | `api.config.artifact_retention.provider_cache_retention_days` | 30 days | last access |
| Package cache | `api.config.artifact_retention.package_cache_retention_days` | 30 days | last access |
| VCS archives | `api.config.vcs.archive_cache_retention_days` | 7 days | write time |

Set a key to `0` to disable that sweep. **A manual eviction reads `0` the other
way round** — see the warning under
[Evicting artifacts in bulk](#evicting-artifacts-in-bulk).

On a sealed node the sweep **skips** the binary, provider and package caches
entirely, because an evicted artifact there could never be refilled. It still
sweeps VCS archives: a sealed node is forbidden from reaching a package registry,
not from reaching your own VCS.

---

## Warming — filling a cache before anything asks

Eviction is the other half of this page; warming is the half that matters before
a node is disconnected. You fill the caches while upstream is still reachable,
then seal the node — and the only way to know you got the set right is to name it
explicitly and check afterwards what landed.

Every artifact kind Terrapod caches can be warmed, from the API, from
`go-terrapod`, from an MCP tool, and from the **Warm package and image caches**
panel on the `/admin/binary-cache` page.

| What | How |
|---|---|
| CLI binaries, provider binaries | `POST /api/v1/admin/binary-cache/warm-bulk` — synchronous, returns per-target results. See [Registry](registry.md#api-endpoints) |
| Packages — `galaxy`, `pypi`, `npm`, `go`, `nuget`, `pulumi` | `POST /api/v1/admin/package-cache/warm` — asynchronous, returns a job id |
| Container images | `POST /api/v1/admin/oci/warm` — asynchronous, returns a job id |
| Either job's progress | `GET /api/v1/admin/warm-jobs/{job_id}` |
| Where a warm would fetch from | `GET /api/v1/admin/package-cache/sources` |

The VCS archive cache is not warmable and has no warm route: an archive is keyed
by a commit SHA and a path set that only a workspace's own configuration
produces, so there is nothing to name ahead of time. It fills itself on the first
poll or run.

### Name versions, not packages

```sh
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  https://terrapod.example.com/api/v1/admin/package-cache/warm \
  -d '{"packages":[
        {"ecosystem":"galaxy","name":"community.general","version":"9.2.0"},
        {"ecosystem":"go","name":"github.com/spf13/cobra","version":"v1.8.1"},
        {"ecosystem":"pulumi","name":"aws","version":"6.0.0"}
      ]}'
# → 202 {"data": {"id": "warm-9f2c…", "links": {"self": "…/admin/warm-jobs/warm-9f2c…"}}}
```

**A version is what makes a warm checkable.** Named at a version, the set you
warmed can afterwards be compared against the lockfile or requirements file your
runs actually pin; "whatever is newest" can be compared against nothing, and on a
node about to be sealed an unverifiable warm is exactly the thing that gets
discovered too late.

Omitting `version` is a convenience and stays supported where the registry
publishes a meaningful newest. **`pulumi` refuses it**: its download protocol has
no index at all — the CLI already knows the version it wants and asks for one
filename — so there is nothing to resolve a bare name against, and the
submission is refused with a 422 saying so rather than accepted and warmed to
something arbitrary.

Each ecosystem names its artifacts its own way, and `name` is that name:

| Ecosystem | `name` | Example |
|---|---|---|
| `galaxy` | `namespace.collection` | `community.general` |
| `pypi` | the project | `requests` |
| `npm` | the package, scope included | `@scope/left-pad` |
| `go` | the module path | `github.com/spf13/cobra` |
| `nuget` | the package id | `Newtonsoft.Json` |
| `pulumi` | `plugin`, or `kind:plugin` | `aws`, `language:python` |
| `oci` | `host/repository:tag` | `quay.io/ansible/awx-ee:24.6.1` |

**Pulumi plugins are published per platform**, so a `pulumi` item takes an
optional `platforms` list of `os/arch` pairs and defaults to the runner's own
`linux/amd64` and `linux/arm64`. Warming one architecture leaves the other to be
discovered after sealing, which is the failure this feature exists to prevent.

### What a warm caches, beyond the artifact

Four of the six ecosystems serve an index document that a sealed node has no
other way to obtain, and warming the artifact alone leaves a cache that looks
full and cannot answer:

| Ecosystem | Also cached | Why it is not optional |
|---|---|---|
| `galaxy` | the collection detail, the version list, and that version's detail | `ansible-galaxy` resolves and reads `download_url` from these before it downloads anything |
| `npm` | the packument | the dependency ranges live there and nowhere else |
| `go` | `@v/list`, and all three of `.info`, `.mod` and `.zip` | the toolchain reads `.mod` to resolve the graph long before it fetches source |
| `nuget` | the versions document | `restore` resolves against it first |
| `pypi` | **every file for the version**, plus PEP 658 metadata sidecars | which wheel pip selects depends on the interpreter and platform doing the installing |
| `pulumi` | nothing else — the client never asks for a list | |

### Warming from somewhere nearer than the public registry

A node being prepared for a restricted network often has a closer copy to pull
from: a sibling Terrapod, or an internal mirror. Declare it once, by **label**:

```yaml
# values.yaml
api:
  config:
    registry:
      package_cache:
        galaxy:
          upstream: https://galaxy.ansible.com
          mirrors:
            hub: https://terrapod.hub.example/api/v1/package-cache/galaxy
```

…and then name that label per submission:

```sh
-d '{"packages":[{"ecosystem":"galaxy","name":"community.general",
                  "version":"9.2.0","source":"hub"}]}'
```

`mirrors` affects **warming only**. Serving always pulls through `upstream`, so
adding one changes nothing about how runs resolve — which is what makes it safe
to add to a running deployment.

**`source` takes a label and never a URL.** `upstream` is the whole of this
proxy's request-forgery surface precisely because it is set in the deployment's
own configuration; a request body carrying a URL would hand an
admin-authenticated caller a server-side fetch to any host they named. An
undeclared label is refused with a 422 that lists the ones that are declared.

`oci` needs none of this and takes no `source`: an image reference's own first
path component names its host, chosen against `registry.oci.upstreams`, so the
per-reference choice was always there.

To see what a submission would do before making one:

```sh
curl -H "$AUTH" https://terrapod.example.com/api/v1/admin/package-cache/sources
# → {"data": [{"id": "galaxy", "attributes": {
#       "ecosystem": "galaxy", "enabled": true,
#       "upstream": "https://galaxy.ansible.com",
#       "mirrors": {"hub": "https://terrapod.hub.example/api/v1/package-cache/galaxy"},
#       "resolves-latest": true}}, …],
#    "meta": {"max-items": 500, "sealed": false}}
```

### Watching the job

A submission returns a job id rather than a result, because a real dependency
closure outlives an HTTP request and a call that times out halfway leaves you
unsure what landed.

```sh
curl -H "$AUTH" https://terrapod.example.com/api/v1/admin/warm-jobs/warm-9f2c…
# → {"data": {"attributes": {
#       "status": "finished", "total": 3, "completed": 3,
#       "succeeded": 2, "failed": 1,
#       "outcomes": [
#         {"ecosystem": "galaxy", "ref": "community.general@9.2.0", "ok": true,
#          "files": 4, "fetched": 4, "source": ""},
#         {"ecosystem": "pypi", "ref": "requests@2.32.3", "ok": true,
#          "files": 6, "fetched": 0, "source": "https://pypi.org"},
#         {"ecosystem": "npm", "ref": "nope", "ok": false,
#          "detail": "404 from upstream", "files": 0, "fetched": 0}]}}}
```

Read the **outcomes**, not the counts. "One failed" across twenty packages is not
something you can act on, and finding the specific gaps before you seal is the
entire purpose. One item failing never stops the rest.

Three outcomes, not two:

- **`fetched` equal to `files`** — this run pulled them.
- **`fetched` 0 with `files` above 0** — it was already cached. Warming is
  idempotent and resumable by design, so re-running is the normal way to close
  the last gaps after a transient upstream failure; without this distinction a
  converged re-run reports exactly what the first run did and you cannot tell
  that it finished.
- **`ok` false** — `detail` says why.

`detail` on a *successful* item names a part that did not land — one architecture
of a Pulumi plugin, say — rather than reporting a clean success for a half-warmed
item.

At most **500 items** per submission, which `meta.max-items` reports; it is a
guard against a paste meant for a different box.

### On a sealed node, warming is refused

```sh
# → 409 {"detail": "This node is sealed (registry.cache_only), so nothing can be
#        fetched from upstream. Warm before sealing, or unseal to warm."}
```

A 409 rather than a job that reports zero successes, because "0 succeeded" on a
sealed node looks exactly like a set of missing packages and would send you
looking upstream for a problem that is on this side.
`GET /admin/package-cache/sources` reports `meta.sealed` as well, so a client can
say warming is unavailable instead of offering a form whose submission can only
fail.

---

## The package cache

### Listing cached artifacts

```sh
curl -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  "https://terrapod.example.com/api/v1/admin/package-cache?ecosystem=galaxy&name=community."
```

Two optional filters:

| Filter | Matching |
|---|---|
| `ecosystem` | exact — one of `galaxy`, `pypi`, `npm`, `go`, `nuget`, `pulumi`. An unrecognised value is not an error; it simply matches nothing. |
| `name` | **prefix** — `community.` returns the whole collection family, and an exact name is the degenerate case of the same query. |

Rows come back **newest-accessed first**, which is the order that answers the
question you are usually asking: what has nothing touched. Sorting by when an
artifact arrived buries exactly that.

The response is an ordinary paged Terrapod collection
([pagination](api-reference.md#pagination)): pass `page[size]` and `page[number]`
to page it, or omit both for the whole filtered set.

**A page costs a page.** The slice and the count are both done in the database,
so asking for fifty artifacts out of sixty thousand reads fifty rows — it used to
fetch every matching row and slice the list in memory, which made each page of a
large cache as expensive as the whole table. `meta.pagination.total-count` is the
whole filtered set rather than the page, so a client knows how much is left.

Omitting both page parameters still returns everything, which is the documented
non-paged contract and what an SDK sending no paging parameters relies on. On a
cache of any size, page it.

Each entry (`data[].type` is `cached-package-files`) carries:

| Attribute | Meaning |
|---|---|
| `ecosystem` | `galaxy` \| `pypi` \| `npm` \| `go` \| `nuget` \| `pulumi` |
| `name` | the package name as that ecosystem normalises it (an npm scope keeps its `/`) |
| `version` | the version this file belongs to |
| `filename` | the file itself. One version routinely has several — an sdist and a wheel per platform and Python ABI — and they are not interchangeable, which is why the artifact is keyed on the filename rather than the version |
| `size` | bytes |
| `digest` | **upstream's own** digest, in the form that ecosystem publishes it (`sha256:…` for PyPI, an SRI `sha512-…` for npm) — not one of ours, so you can match it against what the registry published. Empty when upstream published none |
| `cached-at` | when Terrapod first fetched it |
| `last-accessed-at` | the column retention reads, and the one eviction by age compares |

### Totals per ecosystem

```sh
curl -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  https://terrapod.example.com/api/v1/admin/package-cache/totals
```

```json
{
  "data": [
    { "type": "package-cache-totals", "id": "galaxy",
      "attributes": { "ecosystem": "galaxy", "files": 214, "bytes": 1374389248 } },
    { "type": "package-cache-totals", "id": "pypi",
      "attributes": { "ecosystem": "pypi", "files": 1902, "bytes": 24130437120 } }
  ],
  "meta": { "files": 2116, "bytes": 25504826368 }
}
```

One row per ecosystem, ordered by name, with the grand total in `meta`. **This is
aggregated in the database over the whole cache, not summed from a page** — a
total computed from one page is wrong by however much the page left out, and
wrong in the reassuring direction, which is the opposite of useful to somebody
trying to find what is filling a bucket.

### Evicting one artifact

```sh
curl -X DELETE -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  https://terrapod.example.com/api/v1/admin/package-cache/0193c7b4-2f9e-7a1c-9d84-6b2e5f1a0c33
```

The id is the entry's `id` from the listing — a bare UUID, with no typed prefix.
**404** when the row is already gone, and also when the id is not a well-formed
UUID: a malformed id and an absent artifact are the same answer, because neither
names anything to delete.

### Evicting artifacts in bulk

```sh
# Everything in one ecosystem that nothing has read for a fortnight.
curl -X POST -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H 'Content-Type: application/json' \
  https://terrapod.example.com/api/v1/admin/package-cache/evict \
  -d '{"ecosystem": "npm", "older_than_days": 14}'
# → {"status": "evicted", "files": 318, "bytes": 2104725504, "truncated": false}
```

**One call removes at most 1000 artifacts, and `truncated` says when it stopped
early.** The bound is on the work rather than on what matched: the eviction
deletes one object per artifact, so an unbounded sweep of a large cache runs for
minutes inside one request, and past the gateway's timeout the transaction rolls
back while the objects already deleted stay deleted.

`"truncated": true` means more matched than this call took. **Repeat the same
request to continue** — there is no cursor to carry, because the artifacts that
matched are the ones that went, so the next call starts on what is left. Keep
going until it comes back `false`.

**`older_than_days` is measured from the last access, never from the write.** An
artifact every run pulls would otherwise be evicted merely for being old and
re-fetched immediately, which spends exactly the bandwidth the cache exists to
save.

Both fields are optional and they narrow independently:

- `{"ecosystem": "npm"}` — every npm artifact, whatever its age.
- `{"older_than_days": 90}` — every ecosystem, nothing read in 90 days.
- `{}` — **the whole cache.** That is deliberate rather than a missing guard: it
  is a thing an operator legitimately wants after a bad publish, and nothing is
  lost.

> **`older_than_days: 0` means everything, not "disabled".** The retention keys
> in the table above use `0` to switch a sweep off; here it is a cutoff of *now*,
> so it matches every artifact. If you meant to change nothing, send no filters
> you did not intend rather than a zero.

A misspelled **field** is refused with a 422 rather than silently widening the
eviction — on this endpoint the difference between `ecosystem` and `ecosytem` is
the difference between clearing one registry and clearing the cache. A misspelled
**ecosystem value** is not validated and matches nothing, so it evicts nothing;
check the `files` count in the response rather than assuming a `200` did what you
meant.

---

## The VCS archive cache

A cached archive is a repository tarball, narrowed to the paths some workspace
asked for and addressed by `(connection, owner, repo, commit, path set)`. Two
workspaces in one monorepo that agree on the same path set share one entry.

**It has no database table, and that was a deliberate choice**: the tarball is
content-addressed by commit and path set, so the storage key *is* the record and a
table would be a second copy of it to keep in step. Everything that follows —
the cursor, the age filter, the truncation flag — follows from that, and
[Where the two caches differ](#where-the-two-caches-differ-and-why) collects the
differences in one place.

### Listing cached archives

```sh
curl -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  "https://terrapod.example.com/api/v1/admin/vcs-archive-cache?limit=50"
```

```json
{
  "data": [
    {
      "type": "cached-vcs-archives",
      "id": "vcs_archives/vcs-1b3f/acme/platform/9c1d4b7a3e5f82061d9c4a7b2e8f3015d6a9c47b-4f2e91c7.tar.gz",
      "attributes": {
        "key": "vcs_archives/vcs-1b3f/acme/platform/9c1d4b7a3e5f82061d9c4a7b2e8f3015d6a9c47b-4f2e91c7.tar.gz",
        "connection-id": "vcs-1b3f",
        "owner": "acme",
        "repo": "platform",
        "sha": "9c1d4b7a3e5f82061d9c4a7b2e8f3015d6a9c47b",
        "paths-hash": "4f2e91c7",
        "size": 2847311,
        "last-modified": "2026-11-04T08:12:44Z"
      }
    }
  ],
  "meta": { "next-cursor": "vcs_archives/vcs-1b3f/acme/platform/9c1d4b7a3e5f82061d9c4a7b2e8f3015d6a9c47b-4f2e91c7.tar.gz", "page-size": 50 }
}
```

| Query param | Meaning |
|---|---|
| `connection_id` | narrow to one VCS connection. This narrows the **prefix of the storage walk**, not a page that has already been fetched, so inspecting one connection does not pay for the whole estate |
| `after` | the cursor: return only keys strictly after this one |
| `limit` | entries per page, default 100, clamped to between 1 and the `scan-limit` the totals report (5000) |

| Attribute | Meaning |
|---|---|
| `key` | the storage key, which is also the entry's `id` and the handle an eviction takes |
| `connection-id` | the VCS connection whose credential fetched it. Archives are scoped per connection, so two connections pointing at one repository cannot see each other's entries |
| `owner`, `repo` | the repository, as the key spells it |
| `sha` | the commit the tarball was cut at |
| `paths-hash` | a hash of the path-narrowing set the fetch was scoped to, or the literal **`full`** for a whole-repository archive |
| `size` | bytes |
| `last-modified` | when the object was written — the archive's age, and what eviction by age compares. Empty if the storage backend reports none |

Every field but `size` and `last-modified` is **read out of the key**. A key that
does not parse is therefore still listed, with those fields empty and its real
size: a key this surface cannot read is exactly the one you most need to see,
because nothing else will ever show it to you. Such a key is *not* counted in the
totals below.

### Paging it: `after` and `next-cursor`

There is no `page[number]` here. A page comes from a storage walk, and
`page[size]`-style offsets would re-walk the whole prefix for every page. Pass
the `meta.next-cursor` you were given back as `after` to get the next page:

```sh
CURSOR=""
while : ; do
  PAGE=$(curl -sS -H "Authorization: Bearer $TERRAPOD_TOKEN" \
    "https://terrapod.example.com/api/v1/admin/vcs-archive-cache?limit=500&after=$CURSOR")
  echo "$PAGE" | jq -r '.data[].id'
  CURSOR=$(echo "$PAGE" | jq -r '.meta["next-cursor"] // ""')
  [ -n "$CURSOR" ] || break
done
```

**An empty `next-cursor` means you have reached the end.** It is empty whenever a
page comes back shorter than the `limit` you asked for, so a short page is itself
the end signal; the one case that gives you a cursor with nothing behind it is a
final page that happened to be exactly `limit` long, and the next request then
returns an empty `data` array. `meta.page-size` is how many entries this page
actually holds, not the limit you requested.

This endpoint carries **no `meta.pagination` block** — it is the second
documented exception to Terrapod's pagination convention, alongside the
unbounded run history.

### Totals per repository, and `meta.truncated`

```sh
curl -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  https://terrapod.example.com/api/v1/admin/vcs-archive-cache/totals
```

```json
{
  "data": [
    { "type": "vcs-archive-cache-totals", "id": "vcs-1b3f/acme/platform",
      "attributes": { "connection-id": "vcs-1b3f", "owner": "acme", "repo": "platform",
                      "archives": 1184, "bytes": 3291847168 } }
  ],
  "meta": { "archives": 1612, "bytes": 4017836032, "truncated": true, "scan-limit": 5000 }
}
```

One row per repository, **largest first**, which is the order you want when the
question is what to evict.

`meta.truncated` is the honest half of this endpoint. There are no rows to
aggregate, so the total comes from a storage walk, and a walk has to stop
somewhere — `meta.scan-limit` is where. **`"truncated": true` means the numbers
are a floor**, not a total, and the real figure is larger by an unknown amount. A
total that silently omitted whatever lay past the ceiling would understate
exactly what you came here to find.

When it is true, you have two ways on, and they answer different questions:

- **Which repositories are the problem?** Ask per connection — the walk is
  prefix-scoped, so one connection's archives usually fit well inside the
  ceiling. Combine `connection_id` with the paged listing above and total it up
  yourself; `GET /admin/vcs-archive-cache/totals` itself is not filterable.
- **How do I get the number down?** Evict by age and ask again. At the default
  week of retention the usual cause of a truncated walk is a sweep that has been
  disabled or failing rather than a genuinely enormous estate, so check
  `api.config.vcs.archive_cache_retention_days` and the
  `terrapod_retention_deleted_total` counter
  ([retention metrics](monitoring.md#retention)) before reaching for a bulk
  eviction.

### Evicting one archive

```sh
curl -X DELETE -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  --get --data-urlencode 'key=vcs_archives/vcs-1b3f/acme/platform/9c1d4b7a3e5f82061d9c4a7b2e8f3015d6a9c47b-4f2e91c7.tar.gz' \
  https://terrapod.example.com/api/v1/admin/vcs-archive-cache
```

**The key is a query parameter, not a path segment**, because it contains slashes
and a path would have to encode them.

It is **refused with a 422 unless it sits under the `vcs_archives/` prefix** and
contains no `..`. That guard is not tidiness: this route takes a storage key
straight from a request, and without it a typo — or anything that reached an
admin token — could delete a state version, a plan artifact or a provider binary
through an endpoint whose name says "cache".

The delete is **idempotent**: a well-formed key that is already gone returns the
same `{"status": "evicted"}` as one that was there. There is no row to miss, so
there is nothing a 404 could be reporting. The package-cache delete does 404,
because there it is a row that is absent.

### Evicting archives in bulk

```sh
# Every archive on one connection whose commit is more than a week old.
curl -X POST -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H 'Content-Type: application/json' \
  https://terrapod.example.com/api/v1/admin/vcs-archive-cache/evict \
  -d '{"connection_id": "vcs-1b3f", "older_than_days": 7}'
# → {"status": "evicted", "archives": 406, "bytes": 1178927104,
#    "truncated": true, "next-cursor": "vcs_archives/vcs-1b3f/acme/web/9f2c-full.tar.gz"}
```

**`older_than_days` is measured from the object's write time**, which for an
archive *is* its age: the tarball is immutable once written and there is no
access column to prefer. That makes this eviction cheaper to reason about and
slightly blunter than the package one — an archive a run pulled this morning is
evicted if its commit is old enough, and the next run re-fetches it.

As with the package cache, both fields are optional, neither means everything,
and `older_than_days: 0` means *everything* rather than "disabled".

### Continuing a bounded sweep

**One call considers at most 1000 keys**, and `truncated` says whether more lie
past them. This is a bound on the walk, not on the matches: an age filter may
match a handful of keys in a window of a thousand.

**That is why there is a cursor, and why "just run it again" is not enough.** The
walk starts where you tell it to, so a repeat with no `after` re-examines the
same window — on a cache larger than one window it purges nothing new however
many times you run it, and if nothing in that first window is old enough it never
advances at all. Pass `next-cursor` back as `after`, with **every other field
unchanged**, and the sweep continues:

```sh
# Window 2, resuming exactly where window 1 stopped. Same filters.
curl -X POST -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H 'Content-Type: application/json' \
  https://terrapod.example.com/api/v1/admin/vcs-archive-cache/evict \
  -d '{"connection_id": "vcs-1b3f", "older_than_days": 7,
       "after": "vcs_archives/vcs-1b3f/acme/web/9f2c-full.tar.gz"}'
```

Loop while `truncated` is `true`; an empty `next-cursor` and `"truncated": false`
mean the sweep reached the end. Changing a filter mid-sweep makes the next window
a different question from the last one, so start again rather than adjusting as
you go.

`go-terrapod` reports both on `EvictionResult` (`Truncated`, `NextCursor`) and
the MCP tool reports them as `truncated` and `next_cursor`. There is deliberately
no "evict everything" helper on any of them: the bound is the point, and a helper
that hid it behind one call would put the unbounded work straight back.

> An earlier version of this page said the response did not report truncation
> and told you to run the eviction again until `archives` came back `0`. It did
> not, and that advice did not work — the walk restarted at the same place every
> time. Both are fixed; the cursor is how.

---

## Where the two caches differ, and why

Three differences between the package cache and the VCS archive cache are
deliberate, and each follows from one fact: **the package cache has a row per
artifact and the archive cache has none.** They are not an inconsistency waiting
to be tidied, and expecting either to behave like the other will mislead you.

| | Package cache | VCS archive cache |
|---|---|---|
| **Paging** | `page[size]` / `page[number]`, with `meta.pagination` | a **key cursor** — `after` and `meta.next-cursor`, and **no** `meta.pagination` |
| **Age filter** | **last access** (`last-accessed-at`) | **write time** (`last-modified`) |
| **Totals** | exact, aggregated in the database | a bounded walk, with **`meta.truncated`** when it hit the ceiling |
| **Resuming a bulk eviction** | repeat the same request — the artifacts that matched are the ones that went | pass `next-cursor` back as `after`, or the window restarts in the same place |
| **When the capability is off** | the routes are **not there** | the routes stay |

1. **The archive listing uses a key cursor.** There are no rows to offset into; a
   page comes from a storage walk, and object storage lists keys in
   lexicographic order on every backend Terrapod supports — which is part of the
   storage contract precisely so that a cursor is meaningful. An offset would
   have to re-walk the whole prefix for every page.
2. **The two age filters measure different things.** The package table has an
   access column and retention reads it, because evicting an artifact every run
   pulls merely for being old would re-fetch it immediately. An archive is
   immutable once written and has no access column, so its write time is the only
   age it has. The practical difference: a package nothing has read for a month
   is evictable however recently it was published, and an archive is evictable on
   the age of its commit however recently a run used it.
3. **`meta.truncated` exists because the archive totals cannot be exact.** The
   package totals are a `GROUP BY` over the whole table and need no such flag.
4. **Only the archive eviction has a resume cursor.** Both are bounded per call
   and both report `truncated`, but the package eviction deletes the rows it
   selected, so repeating the request resumes by itself; the archive eviction
   walks keys that are mostly *not* deleted, so without a cursor the same window
   comes back every time.

Two smaller consequences of the same fact, so they do not surprise you: the
archive delete is idempotent where the package delete 404s, and an archive key
that does not parse appears in the listing but not in the totals.

The fourth row has a different cause, and it is worth knowing before you go
looking for a route that is not there. The package cache is an **optional
capability**: with `api.config.registry.package_cache.enabled` set to `false`
every route under `/admin/package-cache` is absent — a 404 as an unrouted path,
not a route that exists and refuses — because that is what a switched-off
capability means everywhere in Terrapod (the container registry behaves the same
way, and `scripts/airgap-gate.sh --gated-off` holds it to that against a running
stack). The admin page says so in place of the section rather than reporting an
error. The VCS archive cache has no such flag — it is part of what Terrapod is,
like the provider mirror and the module registry — so its routes are always
there.

**Nothing is locked away while a capability is off.** Turning one off stops it
serving and stops its background work; everything already cached stays exactly
where it is and is listed again the moment it is turned back on. So the way to
inspect or purge a switched-off package cache is to turn it on, do the work, and
turn it off again — and if the goal is to reclaim the space without serving
anything, the scheduled retention sweep still runs.

---

## Sealed deployments

Set `api.config.registry.cache_only` and Terrapod never attempts an upstream
request. On such a node **the "eviction is cheap" promise does not hold for the
binary, provider and package caches**: an evicted artifact cannot be re-fetched,
and the next run that needs it fails with a 404 naming the setting rather than
being merely slow.

The scheduled sweep knows this and skips those three categories entirely. **The
manual routes do not** — they do exactly what you ask. So on a sealed node, treat
a package-cache eviction as a deletion: confirm you can re-warm it (with
`cache_only` off, or from an internal mirror) before you remove it. See
[Warming](#warming--filling-a-cache-before-anything-asks), and note that warming
is itself refused on a sealed node — so "I will re-warm it afterwards" is only a
plan if you unseal first.

The VCS archive cache is unaffected. Sealing forbids reaching a package registry,
not reaching your own VCS, so an evicted archive is re-fetched by the next poll
or run as normal.

---

## See also

- [Artifact Retention](artifact-retention.md) — the scheduled sweep that should
  be doing this for you, and every other category it covers.
- [Package proxies](package-cache.md) — what fills the package cache, how to
  point a client at it, and how to warm it before sealing.
- [Registry](registry.md) — the binary and provider caches, their upstream
  overrides, and the `/admin/binary-cache` admin page.
- [VCS Integration](vcs-integration.md) — what fills the archive cache, and how
  path narrowing decides which workspaces share an entry.
- [Container registry](oci-registry.md) — the one that is a store as well as a
  mirror, and is garbage-collected rather than evicted.
- [API Reference](api-reference.md#cache-administration) — the eight routes with
  their request and response shapes.
