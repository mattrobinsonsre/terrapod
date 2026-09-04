# The Galaxy surface `ansible-galaxy` consumes

This is the Ansible-side counterpart to
[`tfe-cli-surface.md`](tfe-cli-surface.md): the endpoints the `ansible-galaxy`
client actually calls, and nothing else. Terrapod implements this list. It does
not implement the rest of the Galaxy or Automation Hub API, for the same reason
it implements only the TFE V2 subset the `terraform` CLI consumes — the client
is the contract, not the vendor's full product surface.

Everything below was **captured from a real client**, not read from
documentation. `ansible-galaxy` was pointed at a request-logging stub and driven
through install, publish and verify; the tables record what it asked for. Three
of the findings contradict the obvious reading of the docs, and each is called
out where it appears — the collection path, the publish `task` field, and a
`href` key that looks decorative and is load-bearing.

Re-run the capture with `python3 scripts/galaxy-capture.py`, which is committed
for exactly that purpose.

**Captured with:** `ansible-galaxy` from `ansible-core 2.18.3`, configured with
a `galaxy_server` entry whose `url` ends in `/api/`.

## Install

`ansible-galaxy collection install ns.name`, cold cache:

| # | Request | Purpose |
|---|---|---|
| 1 | `GET /api/` | Version discovery. Response carries `available_versions`, e.g. `{"v3": "v3/"}`. |
| 2 | `GET /api/v3/collections/{ns}/{name}/` | Collection detail. |
| 3 | `GET /api/v3/collections/{ns}/{name}/versions/?limit=100` | Version list, paginated. |
| 4 | `GET /api/v3/collections/{ns}/{name}/versions/{version}/` | Version detail — carries `download_url` and `artifact.sha256`. |
| 5 | `GET <download_url>` | The artifact. An absolute URL of our choosing; the client follows it verbatim. |

Every request carries `Authorization` when the server entry has a `token`.

**The path is `/api/v3/collections/…`, not the `plugin/ansible/content/published`
form.** Galaxy NG serves collections under
`/api/v3/plugin/ansible/content/{repo}/collections/index/{ns}/{name}/`, and that
is what most documentation shows. The client asked for the short form. Implement
what the client asks for.

**A pinned version in `requirements.yml` does not shorten the walk.** An install
from `-r requirements.yml` with `version:` set makes the same five requests. An
earlier capture appeared to skip steps 3 and 4, but that run was served from
`~/.ansible/galaxy_cache`; with the cache cleared the walk is identical. Any
future capture must clear that directory first or it will measure the cache.

### The shapes that matter

Version detail is the one that has to be right, because it is where the client
learns how to fetch and how to check:

```json
{
  "version": "1.0.0",
  "namespace": {"name": "ns"},
  "collection": {"name": "name"},
  "artifact": {"filename": "ns-name-1.0.0.tar.gz", "sha256": "…", "size": 2031},
  "download_url": "https://…/ns-name-1.0.0.tar.gz",
  "metadata": {"dependencies": {}, "tags": []},
  "requires_ansible": ">=2.15",
  "signatures": []
}
```

`artifact.sha256` is verified against the downloaded bytes, so it must be the
digest of exactly what `download_url` serves. `metadata.dependencies` drives
dependency resolution, which is what turns one install into a walk over several
collections.

**`href` is required, on both the collection detail and the version detail.** It
reads as a self-link of the kind a client would ignore, and it is not. Omitting
it aborts the install with

```
[WARNING]: Skipping Galaxy server http://…/api/. Got an unexpected error…
ERROR! Unexpected Exception, this is probably a bug: 'href'
```

— a bare `KeyError` surfaced as a suspected client bug, which is a long way from
pointing at the missing field. Emit it.

## Publish

`ansible-galaxy collection publish ns-name-1.0.0.tar.gz`:

| # | Request | Purpose |
|---|---|---|
| 1 | `GET /api/` | Version discovery, as above. |
| 2 | `POST /api/v3/artifacts/collections/` | `multipart/form-data` upload of the tarball. Responds `202` with `{"task": "<url>"}`. |
| 3 | `GET /api/v3/imports/collections/{id}/` | Import status poll, until `state` is terminal. |

**The `task` URL is not followed.** This is the finding most likely to be got
wrong, because the response field looks like a link. It is not treated as one:
the client takes the **last path segment** of that URL as an import id and
composes `{server}/v3/imports/collections/{id}/` itself.

Proven by returning a task URL sharing no shape with the poll path —
`http://host/totally/elsewhere/abc-123/` — after which the client requested
`/api/v3/imports/collections/abc-123/`. So the id must be the last segment, and
the poll endpoint must exist at that fixed path regardless of what the publish
response says.

`--no-wait` stops after step 2. The default waits, so step 3 is required.

The import status response needs a `state` field; `completed` ends the poll
successfully.

## Verify

`ansible-galaxy collection verify --offline` makes no requests — it checks the
installed tree against its own `MANIFEST.json`. Online verification against a
server is a separate concern from this surface.

## Signatures

Version detail may carry a `signatures` array of `{signature,
pubkey_fingerprint, …}`. The client engages its verification path when
`--keyring` is supplied, checking the detached signature over `MANIFEST.json`.

This is the same trust shape Terrapod already implements for the provider
registry: **the publisher owns the signature and the server verifies it against
a public key already registered with the platform, never re-signing**. That
invariant carries over unchanged — see
[`registry-publishing.md`](registry-publishing.md).

## Deliberately not implemented

Not because they are hard, but because no client on this path asks for them:
the roles API (`/api/v1/roles/`), namespace management as a resource, search,
the Galaxy web UI's own endpoints, and Automation Hub's repository/distribution
model. If a future client turns out to need one, it gets added here first and
implemented second.

## Reproducing the capture

```sh
python3 scripts/galaxy-capture.py
```

[`scripts/galaxy-capture.py`](../scripts/galaxy-capture.py) stands up the stub,
builds a real collection with the real tool, drives install and publish, and
prints every request. It is committed rather than thrown away so a future
`ansible-core` can be **re-captured rather than re-guessed** — three of this
document's statements are things the documentation would have led us to get
wrong, and none of them is safe to assume across a client upgrade.

It clears `~/.ansible/galaxy_cache` before each step itself. Without that the
client answers from its own cache, the server never sees the request, and the
captured walk is silently shorter than the real one.
