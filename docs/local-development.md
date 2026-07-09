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
