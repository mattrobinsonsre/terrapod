# Terrapod Tiltfile - Local Kubernetes Development
# Run with: tilt up --port 10352

# Safety: Only allow local Kubernetes contexts. Never deploy to production.
allow_k8s_contexts([
    'rancher-desktop',
    'minikube',
    'docker-desktop',
    'kind-kind',
    'colima',
    'orbstack',
])

# Ensure namespace exists
local('kubectl create namespace terrapod --dry-run=client -o yaml | kubectl apply -f -')

# ─────────────────────────────────────────────────────────────────────────────
# Local TLS Certificates (mkcert)
# ─────────────────────────────────────────────────────────────────────────────

local_resource(
    'setup-certificates',
    cmd='''
CERT_DIR="$HOME/.local/share/terrapod/certs"
NAMESPACE="terrapod"

if ! command -v mkcert &> /dev/null; then
    echo "ERROR: mkcert is not installed. Run: brew install mkcert && mkcert -install"
    exit 1
fi

mkcert -install 2>/dev/null

mkdir -p "$CERT_DIR"

if [ ! -f "$CERT_DIR/cert.pem" ] || [ ! -f "$CERT_DIR/key.pem" ]; then
    echo "Generating TLS certificates for terrapod.local..."
    cd "$CERT_DIR"
    mkcert -cert-file cert.pem -key-file key.pem \
        "terrapod.local" \
        "localhost" \
        "127.0.0.1"
fi

echo "Creating/updating terrapod-tls-local secret..."
kubectl create secret tls terrapod-tls-local \
    --cert="$CERT_DIR/cert.pem" \
    --key="$CERT_DIR/key.pem" \
    --namespace="$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -

MISSING=""
for HOST in terrapod.local; do
    if ! grep -q "$HOST" /etc/hosts; then
        MISSING="$MISSING $HOST"
    fi
done
if [ -n "$MISSING" ]; then
    echo ""
    echo "WARNING: Missing /etc/hosts entries for:$MISSING"
    echo "Run: sudo sh -c \'echo \"127.0.0.1 terrapod.local\" >> /etc/hosts\'"
    echo ""
fi
''',
    labels=['setup'],
)

# ─────────────────────────────────────────────────────────────────────────────
# Docker Builds
# ─────────────────────────────────────────────────────────────────────────────

# API Server (Python)
docker_build(
    'terrapod-api',
    context='.',
    dockerfile='docker/Dockerfile.api',
    cache_from=['terrapod-api:latest'],
    live_update=[
        sync('./services/terrapod', '/app/terrapod'),
        sync('./alembic', '/app/alembic'),
        run('cd /app && pip install -e .', trigger=['./services/pyproject.toml']),
    ],
)

# Listener (separate image — minimal deps for SSE event loop + K8s Job launcher)
docker_build(
    'terrapod-listener',
    context='.',
    dockerfile='docker/Dockerfile.listener',
    live_update=[
        sync('./services/terrapod/config.py', '/app/terrapod/config.py'),
        sync('./services/terrapod/logging_config.py', '/app/terrapod/logging_config.py'),
        sync('./services/terrapod/http_retry.py', '/app/terrapod/http_retry.py'),
        sync('./services/terrapod/runner', '/app/terrapod/runner'),
    ],
)

# Migrations (separate image — just SQLAlchemy + asyncpg + alembic)
docker_build(
    'terrapod-migrations',
    context='.',
    dockerfile='docker/Dockerfile.migrations',
)

# Runner Job image (python:3.13-slim, pure-Python orchestrator).
# Built as a local_resource (not docker_build) because the runner image is
# referenced in the runners.yaml ConfigMap, not in a pod spec — Tilt's image
# injection doesn't apply.  values-local.yaml sets terrapod-runner:local with
# pullPolicy: Never so K8s Jobs find it in the local Docker daemon.
local_resource(
    'build-runner-image',
    cmd='docker build -f docker/Dockerfile.runner -t terrapod-runner:local .',
    deps=[
        'docker/Dockerfile.runner',
        'services/pyproject-runner.toml',
        'services/terrapod/http_retry.py',
        'services/terrapod/runner/__init__.py',
        'services/terrapod/runner/runner_config.py',
        'services/terrapod/runner/download.py',
        'services/terrapod/runner/exec_subprocess.py',
        'services/terrapod/runner/lock_extender.py',
        'services/terrapod/runner/plan_artifacts.py',
        'services/terrapod/runner/job_entrypoint.py',
        'services/terrapod/runner/phases',
    ],
    labels=['build'],
)

# Web UI (Next.js) — use builder stage for dev mode with hot reload
docker_build(
    'terrapod-web',
    context='.',
    dockerfile='docker/Dockerfile.web',
    target='builder',
    entrypoint=['npx', 'next', 'dev', '-H', '0.0.0.0'],
    live_update=[
        sync('./web/src', '/app/src'),
        sync('./web/public', '/app/public'),
        sync('./web/next.config.js', '/app/next.config.js'),
    ],
)

# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure (PostgreSQL + Redis)
# ─────────────────────────────────────────────────────────────────────────────

# PostgreSQL for local development.
#
# Data lives on a PVC so workspaces, runs, registry contents, etc.
# survive `tilt down` / `tilt up` cycles and pod recreations.
# `Recreate` strategy (rather than RollingUpdate) avoids two pods ever
# trying to mount the same RWO PVC at once.
#
# Redis is *deliberately* left ephemeral below — sessions, listener
# heartbeats, and scheduler locks should reset on Tilt restart.
# PostgreSQL + Redis are deployed by the Helm chart itself now
# (postgresql.deploy / redis.deploy = true in values-local.yaml), so the Tilt
# dev loop and the `make eval` kind/k3d quickstart share ONE batteries-included
# datastore path instead of maintaining a separate copy here. Postgres keeps a
# PVC (persists across restarts via embedded.persistence); Redis is ephemeral.
#
# These resources come through helm() below. Tilt does NOT execute helm hook
# ordering, so the migrations→Postgres and api→migrations sequencing is enforced
# by the resource_deps wired further down (unchanged — same resource names).
k8s_resource('terrapod-postgresql', labels=['infra'])
k8s_resource('terrapod-redis', labels=['infra'])

# ─────────────────────────────────────────────────────────────────────────────
# Kubernetes Resources
# ─────────────────────────────────────────────────────────────────────────────

# Load Helm chart with local overrides
# Explicit watch_file() calls ensure Tilt re-renders the chart when values
# or templates change. The helm() function should do this automatically but
# in practice it's unreliable (see tilt-dev/tilt#5932).
watch_file('helm/terrapod/values.yaml')
watch_file('helm/terrapod/values-local.yaml')
watch_file('helm/terrapod/templates')

k8s_yaml(helm(
    'helm/terrapod',
    name='terrapod',
    namespace='terrapod',
    values=['helm/terrapod/values.yaml', 'helm/terrapod/values-local.yaml'],
    set=[
        'api.image.repository=terrapod-api',
        'api.image.tag=latest',
        'api.image.pullPolicy=Never',
        'listener.image.repository=terrapod-listener',
        'listener.image.tag=latest',
        'listener.image.pullPolicy=Never',
        'migrations.image.repository=terrapod-migrations',
        'migrations.image.tag=latest',
        'migrations.image.pullPolicy=Never',
        'web.image.repository=terrapod-web',
        'web.image.tag=latest',
        'web.image.pullPolicy=Never',
        'web.enabled=true',
        'ingress.enabled=true',
    ],
))

# ─────────────────────────────────────────────────────────────────────────────
# Resource Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Migrations job (name includes revision suffix from Helm)
k8s_resource(
    'terrapod-migrations-1',
    labels=['jobs'],
    resource_deps=['terrapod-postgresql'],
)

# Bootstrap job (creates initial admin user, runs on first install)
k8s_resource(
    'terrapod-bootstrap-1',
    labels=['jobs'],
    resource_deps=['terrapod-migrations-1'],
)

# API Server (accessed via Ingress at https://terrapod.local)
k8s_resource(
    'terrapod-api',
    labels=['backend'],
    resource_deps=['terrapod-postgresql', 'terrapod-migrations-1', 'setup-certificates'],
)

# Web UI (accessed via Ingress at https://terrapod.local)
k8s_resource(
    'terrapod-web',
    labels=['frontend'],
    resource_deps=['terrapod-api'],
    links=[link('https://terrapod.local', 'Terrapod')],
)

# Dev pool setup — creates pool + join token via the API pod's bootstrap script.
# Idempotent: skips if pool already exists.
local_resource(
    'setup-dev-pool',
    cmd='''
echo "Waiting for API pod to be ready..."
kubectl -n terrapod wait --for=condition=Ready pod -l app.kubernetes.io/name=terrapod,app.kubernetes.io/component=api --timeout=120s

echo "Creating dev pool + join token..."
kubectl -n terrapod exec deploy/terrapod-api -- env \
    DATABASE_URL="postgresql+asyncpg://terrapod:terrapod@terrapod-postgresql:5432/terrapod" \
    TERRAPOD_BOOTSTRAP_ADMIN_EMAIL=admin \
    TERRAPOD_BOOTSTRAP_ADMIN_PASSWORD=admin \
    TERRAPOD_BOOTSTRAP_POOL_NAME=dev \
    TERRAPOD_BOOTSTRAP_POOL_TOKEN=dev-join-token-do-not-use-in-prod \
    python -m terrapod.cli.bootstrap
''',
    labels=['setup'],
    resource_deps=['terrapod-api'],
)

# Runner Listener (uses same image as API, needs dev pool)
k8s_resource(
    'terrapod-listener',
    labels=['backend'],
    resource_deps=['setup-dev-pool'],
)

# ─────────────────────────────────────────────────────────────────────────────
# Local Commands
# ─────────────────────────────────────────────────────────────────────────────

# Run Python tests (containerized)
local_resource(
    'test-python',
    cmd='docker compose -f docker-compose.test.yml run --rm --build test',
    labels=['tests'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)

# Run linters (containerized)
local_resource(
    'lint',
    cmd='docker compose -f docker-compose.test.yml run --rm --build lint',
    labels=['tests'],
    auto_init=False,
    trigger_mode=TRIGGER_MODE_MANUAL,
)
