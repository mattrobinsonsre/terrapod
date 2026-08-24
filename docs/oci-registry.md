# Container registry

Terrapod serves an [OCI distribution](https://github.com/opencontainers/distribution-spec)
registry at `/v2/`, so the images your runs need can live in the same place as
your state, modules and providers — behind the same auth, on the same storage,
inside the same network boundary.

The prefix is not a Terrapod choice. The distribution spec mandates `/v2/`, which
is why this is the one API surface that does not live under `/api/`.

> **Status.** Push, pull, cross-repository mount, tag listing, pull-through
> mirroring and the referrers API are implemented and pass the OCI conformance
> suite (pull, push and content discovery). Deleting content through the API is
> deliberately not offered — see [Deletion](#deletion).

## Why it is here

Terrapod is built for networks that cannot reach the internet. In one of those,
"just pull it from Docker Hub" is not available, and the usual answer — stand up
a separate registry, with its own deployment, its own storage, its own
credentials and its own upgrade path — adds a second system to the two or three
you were already trying to keep alive.

Since Terrapod already holds artifacts, already authenticates every caller, and
already knows how to be a pull-through cache for providers and binaries, serving
images is the same job again rather than a new one.

## Quick start

The registry is on by default. Any Terrapod credential works as the password;
the username is ignored, which is the same convention GHCR uses.

```sh
# Any API token, or the token from `tofu login`.
docker login terrapod.example.com -u anything -p "$TERRAPOD_TOKEN"

docker tag my-image terrapod.example.com/platform/my-image:v1
docker push terrapod.example.com/platform/my-image:v1
docker pull terrapod.example.com/platform/my-image:v1
```

`skopeo` and `crane` work the same way. So does a Kubernetes `imagePullSecret`:

```sh
kubectl create secret docker-registry terrapod \
  --docker-server=terrapod.example.com \
  --docker-username=anything \
  --docker-password="$TERRAPOD_TOKEN"
```

Basic auth is what makes that work without a token service. A
`kubernetes.io/dockerconfigjson` secret carries a username and a password and
nothing else, and the spec permits Basic — so the kubelet can pull with an
ordinary Terrapod credential, including a short-lived runner token scoped to a
single run.

## Authentication and access

**Nothing is anonymous.** Every request needs a credential, including reads.
This is deliberate and worth being explicit about: a registry that mirrors
upstream content and answers anonymously is an open pull-through cache for
whoever finds it, serving your bandwidth and your storage to strangers.

Repositories use the same label-based RBAC as the rest of Terrapod — the
`registry:read`, `registry:write` and `registry:admin` capabilities, resolved
through owner, labels and roles exactly as modules and providers are. A
repository is created by the first push that is allowed to make it, and its
creator becomes the owner.

## Pull-through mirroring

A repository whose first path component names a configured upstream is a mirror:
pulling `terrapod.example.com/quay.io/ansible/awx-ee` fetches
`quay.io/ansible/awx-ee` once, stores it, and serves every later pull locally.

Upstreams are an **allow-list, not a convenience**. Without one, a client could
name any host and make Terrapod fetch from it — a server-side request forgery
primitive. An empty list means push-only, which is the correct setting for an
air-gapped install.

```yaml
api:
  config:
    registry:
      oci:
        enabled: true
        upstreams:
          - host: quay.io                      # anonymous
          - host: registry.example.com         # authenticated
            username: robot
            existingSecret: terrapod-upstream-example
            existingSecretKey: password        # optional, defaults to "password"
```

An upstream password is **never** written into values or a ConfigMap. It comes
from a Secret, injected as `TERRAPOD_OCI_UPSTREAM_{HOST}_PASSWORD` (the host
uppercased, with non-alphanumeric characters replaced by underscores).

`registry.cache_only` seals upstream fetching across every Terrapod cache
including this one, and takes precedence over anything listed here.

## Storage

Blobs are content-addressed and stored **once per deployment**, with a link row
per repository that holds them. Two repositories sharing a base image share the
bytes, and a cross-repository mount is a link rather than a copy — which is why
`docker push` of an image whose layers are already present finishes almost
immediately.

Membership is still checked per repository. A blob that exists somewhere in the
deployment is not readable from a repository that has not been given it, so
content addressing never becomes a cross-repository read.

## Referrers

The referrers API (`GET /v2/<name>/referrers/<digest>`) lists manifests attached
to a subject — signatures, SBOMs, attestations. It matters more here than in a
general-purpose registry: provenance for an air-gapped estate has to travel
*with* the content, because nothing inside the network can reach out to check
anything.

Filtering by `?artifactType=` is supported, and a filtered response carries
`OCI-Filters-Applied: artifactType` so a client can tell a filtered list from a
registry that ignored the filter.

A subject with nothing attached returns an **empty index**, never a 404 — a 404
tells a client the endpoint is unsupported and sends it down a legacy fallback
path.

## Deletion

**An image you pushed stays until you remove it.** A registry is not a cache for
content you put in it: nothing here expires a pushed image because it has gone
quiet, any more than a registry you pay for would. Reclaiming that space is a
deliberate act, not a background sweep.

Removing a pushed image is **not yet possible** — there is no delete API — so a
pushed image is currently permanent. That is the honest position and it is
tracked in [#1423](https://github.com/mattrobinsonsre/terrapod/issues/1423); the
collector below is the half that already exists, and deletion is the half that
will use it.

Deleting a manifest reclaims nothing on its own, incidentally, because its layers
are usually shared with other images. That is why the two are separate: deletion
un-references content, and **garbage collection** frees whatever nothing points at
any more. It runs hourly and needs no downtime.

### What is collected, and what never is

A blob is collected when no manifest references it any more. That is the only
condition, and it is what makes the shared-layer case safe: a base layer two
images share stays until the second of them goes.

In practice that means collection reclaims **superseded mirrored content** —
layers left behind when a tag moved upstream — and **abandoned uploads**, blobs
from a push that never sent its manifest.

**An image you pushed is never expired by age.** It exists nowhere else, and a
registry that quietly eats the images you put in it is not a registry. Only its
genuinely unreferenced blobs are ever collected.

**Nor is a mirrored one**, which is the opposite of what a disk-usage instinct
suggests. Deleting a cached image on a schedule cannot make anything fresher —
the next pull asks upstream regardless — and it discards the copy that keeps this
cache answering at the moment upstream is unreachable. For a registry whose
reason to exist is restricted networks, that is exactly backwards: a stale image
that still runs beats a correct one you cannot fetch.

Freshness is handled where it belongs, on the read path. A mirrored tag past its
TTL is revalidated when somebody pulls it, replaced only once upstream confirms a
new digest, and served stale if upstream cannot be reached. Nothing is deleted to
achieve it — the superseded manifest simply stops being referenced.

**And a layer is never evicted on its own terms.** It lives exactly as long as
some manifest references it. Layers are shared by construction, so age or
last-use would say nothing useful about one: the base image everything is built
on is the most-served object in the store and is also reachable from images
nobody has pulled in months. Removing it because it looked idle would break every
image that needs it.

**Signatures, SBOMs and attestations survive.** A referrer usually carries no tag
of its own, so a naive "untagged means unreachable" sweep would destroy
provenance while leaving the image it describes — the worst half to lose in an
air-gapped estate, where nothing can fetch it again and the image still pulls, so
the loss is silent. Referrers of a reachable image are treated as reachable.

### A push in flight is safe

Blobs are uploaded *before* the manifest that references them, so for a moment
during every push there is content that nothing points at. Collection ignores
anything that arrived within `registry.oci.gc.grace_hours` (24 by default), so a
push in progress is never a candidate.

Docker's own registry answers the same problem by requiring read-only mode during
collection. A grace window costs nothing and needs no downtime. Raise it if
pushes in your environment can legitimately take longer than a day.

The window is measured from when content arrived **in that repository**, not from
when the blob was first created — a cross-repository mount of a months-old layer
is a push in flight too.

### Watching it

`terrapod_oci_gc_bytes_reclaimed_total` is the one to graph: a blob count tells
you a collection ran, bytes tell you whether it helped.
`terrapod_oci_gc_errors_total` counts repositories a cycle declined to collect
because it could not read a manifest and therefore could not be sure what was
reachable — erring toward keeping bytes.

What *is* reaped automatically is abandoned uploads. A push that starts and dies
leaves chunks in object storage, and anyone with push access could repeat that
until the disk filled; the reaper removes sessions untouched for longer than
`upload_session_timeout_hours` (24 by default). The window is generous on
purpose — a slow, resumed push over a poor link is legitimate, and reaping one
mid-flight would destroy real work.

## Configuration

| Value | Default | What it does |
|---|---|---|
| `api.config.registry.oci.enabled` | `true` | Serve the registry at `/v2/`. Disabling hides the surface; pushed images stay in storage and reappear if re-enabled. |
| `api.config.registry.oci.upstreams` | `[]` | The pull-through allow-list. Empty means push-only. |
| `api.config.registry.oci.upload_session_timeout_hours` | `24` | How long an in-progress upload may sit untouched before it is reaped with its chunks. |
| `api.config.registry.oci.gc.enabled` | `true` | Collect unreferenced blobs. Off means the registry only ever grows — there is no delete API. |
| `api.config.registry.oci.gc.grace_hours` | `24` | How long newly-arrived content is protected, so a push in flight is never collected. |
| `api.config.registry.cache_only` | `false` | Seals upstream fetching for every cache, this one included. |

## Verifying a deployment

The OCI conformance suite runs against Terrapod in CI on every change, and you
can point it at your own deployment:

```sh
scripts/oci-conformance.sh https://terrapod.example.com
```

It is worth running against the address your clients use rather than against the
API directly. Terrapod routes all traffic through the frontend proxy, and a
problem on that hop — a stripped header, a redirect — is invisible to a test
that skips it.
