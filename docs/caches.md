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
`/admin/binary-cache` page, and two **read-only** MCP tools —
`terrapod_package_cache_list` and `terrapod_vcs_archive_cache_list` ([MCP](mcp.md)).
The evictions are deliberately **not** offered as MCP tools: an eviction's only
result is a slower run later for somebody who did not ask, which is an operator's
call rather than an agent's.

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
# → {"status": "evicted", "files": 318, "bytes": 2104725504}
```

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
# → {"status": "evicted", "archives": 406, "bytes": 1178927104}
```

**`older_than_days` is measured from the object's write time**, which for an
archive *is* its age: the tarball is immutable once written and there is no
access column to prefer. That makes this eviction cheaper to reason about and
slightly blunter than the package one — an archive a run pulled this morning is
evicted if its commit is old enough, and the next run re-fetches it.

As with the package cache, both fields are optional, neither means everything,
and `older_than_days: 0` means *everything* rather than "disabled".

**One call considers at most `scan-limit` archives**, the same ceiling the totals
report. The response says how many it removed but not whether it stopped early,
so on a large estate **run it again until `archives` comes back `0`**.

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

Two smaller consequences of the same fact, so they do not surprise you: the
archive delete is idempotent where the package delete 404s, and an archive key
that does not parse appears in the listing but not in the totals.

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
[Warming ahead of a seal](package-cache.md#warming-ahead-of-a-seal).

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
