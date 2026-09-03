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

kind shares the two-store problem but not the fix: `kind load docker-image` is
reliable, so use that rather than a registry.

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

For the architecture and conventions behind the stack, see
[Architecture](architecture.md) and the repository `AGENTS.md`.
