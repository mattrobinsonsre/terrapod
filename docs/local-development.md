# Local Development

This page is for **contributors** who want to run Terrapod from source on a
local Kubernetes cluster. If you just want to **deploy and use** Terrapod, see
[Getting Started](getting-started.md) — you do not need any of this.

Terrapod's local dev stack is driven by [Tilt](https://tilt.dev/): it builds the
images, deploys the Helm chart against a local cluster, runs the database
migrations, bootstraps an admin user, and live-syncs source changes into the
running pods.

## Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| Docker | Container runtime | [docker.com](https://www.docker.com/) |
| A local Kubernetes cluster | Run the stack | Rancher Desktop, Docker Desktop (K8s enabled), minikube, kind, OrbStack, or colima |
| Tilt | Local dev orchestration | `brew install tilt` |
| Go, GPG, ZIP tools, and local Git tags | Build and sign the local Terrapod provider | Go version in `provider/go.mod`; `gpg`, `zip`, `unzip`, `sha256sum`; fetch tags after cloning |
| mkcert | Local TLS certificate | `brew install mkcert` |
| tofu (recommended) or terraform | Exercise the CLI flows | [opentofu.org](https://opentofu.org/) |

The local stack is deliberately pinned to its own namespace (`terrapod`), Tilt
port (`10352`), and hostname (`terrapod.local`) so it can run alongside other
local projects.

## Setup

### 1. Local CA + hosts entry

```zsh
brew install mkcert && mkcert -install
sudo sh -c 'echo "127.0.0.1 terrapod.local" >> /etc/hosts'
```

`mkcert -install` adds a local CA to your system trust store so
`https://terrapod.local` is trusted by your browser and the terraform/tofu CLI.

### 2. Start the stack

```zsh
make dev          # runs `tilt up --port 10352`
```

This creates the `terrapod` namespace, generates the TLS cert, deploys
PostgreSQL and Redis in-cluster, builds the API/web images, runs the Alembic
migrations, bootstraps the admin user, and deploys the API, web UI, and runner
listener. Watch progress in the Tilt UI at <http://localhost:10352>.

Tilt also runs `provider/GNUmakefile` against the current Go source and signs
a Linux provider ZIP. The `-dev.<number>` version is based on the latest fetched
Git version tag and source content (override the tag with
`TERRAPOD_DEV_PROVIDER_TAG=v1.9.0 make dev`). The persistent dev-only GPG home
lives at `~/.local/share/terrapod/certs/provider-gnupg/`, separate from the
local TLS cert and key in the same parent directory. An existing signing key
in `provider/.dev-build/gnupg/` is moved there after its public key is checked;
it is never silently replaced. The exported public key and immutable signed
artifacts stay in the ignored `provider/.dev-build/`; `current-version`
records the version just built. That directory is also excluded from Docker
build contexts. Tilt copies only the signed ZIP, checksums, signature, and
public key into the filesystem cache PVC using a short-lived helper pod (not
an API upload), then starts the API with that version and mounts the public
key. The private key never enters the cluster. After a successful signed build,
Tilt generates `provider/.dev-build/terrapod_self_contained-test/main.tf` from
the checked-in self-contained example, stamping it with the exact version in
`provider/.dev-build/current-version`. Upload the generated directory as a new
module version when testing; it is never uploaded automatically. The checked-in
source remains unpinned. OpenTofu selects prerelease providers only with an
**exact** constraint, so uploading the unpinned source will not install Tilt's
`-dev.<number>` provider. Publish a new module version after a provider rebuild;
editing a local file does not change an already-uploaded archive.

Create a module slot in the private registry, then upload the generated directory
(not `provider/examples/modules/terrapod_self_contained/`) with
[`terrapod-publish module`](registry-publishing.md#publishing-a-module):

```sh
terrapod-publish module \
  --host terrapod.local \
  --name terrapod_self_contained \
  --provider terrapod \
  --version 0.1.0 \
  --source provider/.dev-build/terrapod_self_contained-test
```

Use the name and provider of your existing module slot and an unused module
version each time the dev provider changes; the module version is separate from
the pinned provider version. Authenticate with a token that has registry `write`
on that slot; see the [publisher's authentication instructions](registry-publishing.md#auth-resolution).
The signed provider artifacts are staged by Tilt and do not need a separate
module upload.

In this local profile the listener waits for the web service and sends its API
requests through the in-cluster BFF. Runner Jobs use the same URL to redirect
`terrapod.local` CLI provider-registry requests. The local profile also sets
`TERRAPOD_ALLOW_INSECURE_TRANSPORT=1` on runner Jobs so the Terrapod provider
can call that HTTP BFF; this sends its bearer token in cleartext inside the
dev cluster and must not be copied to production. The CLI registry host override
already accepts HTTP; the environment variable applies to the provider's own
API calls, not to provider installation.

Tear it down with `make dev-down`.

### 3. Access

Open <https://terrapod.local>. The bootstrap job creates an admin user — the
default local credentials are `admin` / `admin` (set in
`helm/terrapod/values-local.yaml`). Check the `terrapod-bootstrap-1` resource in
the Tilt UI to confirm it ran.

From there the workflow is the same as a real deployment — see
[Getting Started](getting-started.md) for creating a workspace and running your
first plan/apply (substitute `terrapod.local` for the hostname).

## Nested clusters (k3d / kind)

**Skip this if your cluster shares an image store with your local builds** —
Rancher Desktop, Docker Desktop and minikube's Docker driver all do, and an
image you build is immediately visible to pods.

k3d and kind run the cluster *inside* a container with its own containerd, so
**the host's image store and the cluster's are separate**. An image built
locally is invisible to pods until it is actively delivered, and because the
chart runs the local images with `imagePullPolicy: Never`, a missing image is a
hard `ErrImageNeverPull` rather than a pull from a registry. Confirm which store
an image is in:

```zsh
docker images | grep terrapod                         # the HOST's store
docker exec k3d-local-server-0 crictl images | grep terrapod   # the CLUSTER's
```

The Tiltfile handles delivery automatically on a `k3d-*` context by pushing
through a registry, which needs a one-off setup. Delivery via a registry rather
than `k3d image import` is a deliberate choice — measured on a 775 MB image
after a one-line source change, a push moves only the changed layer (~0.2s)
while an import copies the whole image every time (~11.5s), and that cost is
paid on every edit across five images.

```zsh
k3d registry create tp-registry --port 5111

k3d cluster create local --no-lb --api-port 6443 \
  --registry-use k3d-tp-registry:5111 \
  -p "80:80@server:0:direct" -p "443:443@server:0:direct"
```

Two details in that command are load-bearing if you recreate the cluster:

- **`--no-lb`** drops k3d's load balancer, whose nginx resolves upstreams once
  at startup and so caches a stale server IP across a cluster restart.
- **`:direct`** on the port mappings is then required — without the load
  balancer a plain `@server:0` mapping is rejected as a proxy-type mapping.

The Tiltfile pushes to `localhost:5111` (how the **host** reaches the registry)
and rewrites image references to `k3d-tp-registry:5111` (how the **node**
resolves the same registry). Nothing here applies on any other context, so a
Docker/Rancher/minikube setup is untouched.

On `kind-kind`, Tilt loads the runner Job image into the kind node after each
local build with `kind load docker-image terrapod-runner:local --name kind`.
Tilt delivers the other images automatically; no manual import is needed to
run catalog or workspace jobs.

## When the container store fills up

`make doctor` reports the health of the local container store. Run it if builds
start failing oddly, and occasionally regardless.

It exists because of a failure mode that is invisible until it is expensive.
`podman build` writes a layer's content to disk and then commits its metadata.
Interrupt it in between — a killed build, a step that times out, a full disk —
and the layer stays on disk with nothing referencing it.

**Nothing reclaims those layers.** `podman system prune` only removes objects
podman has metadata for, and `podman system check --repair` cannot see a layer
it has no record of either; it reports zero orphans even at `--max 1m`. The
space is unrecoverable short of a full storage reset.

It also accelerates. As the disk fills, metadata writes begin to fail, which
orphans layers that would otherwise have committed cleanly — so the problem
feeds itself, and every individual symptom looks like an ordinary build failure.

One workstation reached **9,415 orphaned layers holding roughly 85 GB** before
anyone noticed, on a 120 GB store that `podman system df` was cheerfully
reporting as 19 GB of images.

Two guards exist:

- `scripts/lib.sh` refuses to build the test image below
  `CONTAINER_STORE_MIN_FREE_GB` (default 10). Failing loudly beats filling the
  store silently. Skipped in CI, where the runner is disposable.
- `make doctor` compares layer directories on disk against
  `overlay-layers/layers.json`. That difference is the number that matters, and
  it is the one no built-in command shows you.

If you are already deep in it, the only complete fix is:

```zsh
podman system reset      # destroys ALL images, containers and volumes
```

That includes your k3d cluster, so recreate it with the commands in *Nested
clusters (k3d / kind)* above, then `make dev`.

Damaged *tracked* layers are a different and much smaller problem — those do
have metadata, so `podman system check --repair` genuinely fixes them.

## Day-to-day

- **Live reload** — `tilt up` live-syncs `services/terrapod` and `web/src` into
  the running pods; the API auto-reloads (uvicorn) and the web hot-reloads
  (Next.js). If a change doesn't take, force it:
  `tilt trigger --port 10352 terrapod-api` (or `terrapod-web`).
- **Migrations** — after adding an Alembic revision, trigger the migration job:
  `tilt trigger --port 10352 terrapod-migrations-1`.
- **Tests & lint** (containerised — no local Python needed):
  `make test`, `make lint`. Tear down test containers with `make test-down`.
- **Never** use `kubectl cp`, `kubectl apply`, or `docker build` against
  Tilt-managed resources — it corrupts Tilt's state. Let Tilt manage its
  resources; use `tilt trigger` to force a rebuild.
- **The Slack integration is off in the dev stack**, because its tokens come
  from a Secret you have to create by hand and the stack should come up without
  one. To work on that surface, create the Secret as
  [`slack-integration.md`](slack-integration.md) describes, then set
  `api.config.slack.enabled: true` and `existingSecret: terrapod-slack` in
  `helm/terrapod/values-local.yaml` — the Secret's name is already written
  there, so it is a one-line change. Socket Mode means no ingress or tunnel is
  needed for the dev loop.

## The SSH target, for driving a real configure

The stack ships a throwaway SSH server so a [configure](ansible-configure.md) can
connect to something real rather than to loopback. Two Tilt resources bring it
up — `setup-ssh-target-key` generates a keypair and the Secret the server trusts,
and `ssh-target` is the server itself:

```sh
tilt trigger --port 10352 setup-ssh-target-key
tilt trigger --port 10352 ssh-target
```

The keypair lands in `~/.local/share/terrapod/ssh-target/`. **No key material is
in the repository** — the pair is generated on first use and the server's host
key at image build time, because a private key in a public repository is a
secret-scanner finding whatever its intended lifetime.

To point a workspace at it, create an `ansible_machine_auth` credential whose
`private_key` is that file and whose `known_hosts` is the server's host key:

```sh
kubectl -n terrapod exec deploy/ssh-target -- cat /etc/ssh/ssh_host_ed25519_key.pub
```

prefixed with the hostname the inventory uses, which is the Service name:

```yaml
all:
  hosts:
    ssh-target:
      ansible_user: target
      ansible_ssh_private_key_file: "${terrapod:ssh-target-key.private_key}"
      ansible_ssh_common_args: "-o UserKnownHostsFile=${terrapod:ssh-target-key.known_hosts}"
```

Omit the `known_hosts` half and the configure fails `UNREACHABLE` with `Host key
verification failed`, which is the behaviour rather than a fault — see [Host-key
verification](ansible-configure.md#host-key-verification-and-how-to-satisfy-it).

**It is a managed node, not just a shell**: it carries `python3`, so a real
ansible module stages and runs on it. That matters because module staging is
where `remote_tmp` is resolved, and `ansible.builtin.raw` reaches none of it.
`scripts/ssh-target-gate.sh` exercises the same server at container level with
no Kubernetes, which is the quicker loop when only the SSH behaviour is in
question.

For the architecture and conventions behind the stack, see
[Architecture](architecture.md) and the repository `AGENTS.md`.
