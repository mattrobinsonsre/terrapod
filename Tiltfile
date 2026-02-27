# Terrapod Tiltfile - Local Kubernetes Development
# Run with: tilt up --port 10352
#
# Does NOT conflict with ~/code/bamf or ~/code/kubamf:
#   - Different namespace (terrapod vs bamf vs kubamf)
#   - Different Tilt UI port (10352 vs 10350)
#   - Different hostname (terrapod.local vs bamf.local)

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
    live_update=[
        sync('./services/terrapod', '/app/terrapod'),
        run('cd /app && pip install -e .', trigger=['./services/pyproject.toml']),
    ],
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
    ],
)

# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure (PostgreSQL + Redis)
# ─────────────────────────────────────────────────────────────────────────────

# PostgreSQL for local development
k8s_yaml(blob("""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: terrapod-postgresql
  namespace: terrapod
spec:
  replicas: 1
  selector:
    matchLabels:
      app: terrapod-postgresql
  template:
    metadata:
      labels:
        app: terrapod-postgresql
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          ports:
            - containerPort: 5432
          env:
            - name: POSTGRES_USER
              value: terrapod
            - name: POSTGRES_PASSWORD
              value: terrapod
            - name: POSTGRES_DB
              value: terrapod
---
apiVersion: v1
kind: Service
metadata:
  name: terrapod-postgresql
  namespace: terrapod
spec:
  selector:
    app: terrapod-postgresql
  ports:
    - port: 5432
      targetPort: 5432
"""))

# Redis for local development
k8s_yaml(blob("""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: terrapod-redis
  namespace: terrapod
spec:
  replicas: 1
  selector:
    matchLabels:
      app: terrapod-redis
  template:
    metadata:
      labels:
        app: terrapod-redis
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          ports:
            - containerPort: 6379
---
apiVersion: v1
kind: Service
metadata:
  name: terrapod-redis
  namespace: terrapod
spec:
  selector:
    app: terrapod-redis
  ports:
    - port: 6379
      targetPort: 6379
"""))

k8s_resource('terrapod-postgresql', labels=['infra'])
k8s_resource('terrapod-redis', labels=['infra'])

# ─────────────────────────────────────────────────────────────────────────────
# Kubernetes Resources
# ─────────────────────────────────────────────────────────────────────────────

# Load Helm chart with local overrides
k8s_yaml(helm(
    'helm/terrapod',
    name='terrapod',
    namespace='terrapod',
    values=['helm/terrapod/values.yaml', 'helm/terrapod/values-local.yaml'],
    set=[
        'api.image.repository=terrapod-api',
        'api.image.tag=latest',
        'api.image.pullPolicy=Never',
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
