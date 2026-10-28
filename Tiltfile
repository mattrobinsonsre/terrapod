# Terrapod Tiltfile - Local Kubernetes Development
# Run with: tilt up --port 10352

# Safety: Only allow local Kubernetes contexts. Never deploy to production.
allow_k8s_contexts([
    'rancher-desktop',
    'k3d-local',
    'minikube',
    'docker-desktop',
    'kind-kind',
    'colima',
    'orbstack',
])

# Image delivery.
#
# On a nested cluster (k3d/kind) the host's image store and the cluster's are
# SEPARATE, so an image built locally is invisible to pods until it is actively
# delivered. Rancher Desktop shared one store via cri-dockerd and needed none of
# this; it was the outlier, and on this workstation it is gone.
#
# k3d uses a registry; kind imports the runner Job image after its local build.
# Docker/Rancher/minikube setups are untouched.
#
# Delivered through a local registry rather than `k3d image import`, measured on
# a 775 MB image after a one-line source change:
#
#     registry push   0.20s
#     k3d import     11.5s
#
# The import is flat because it copies the whole image every time; the push
# moves only the changed layer. That is paid on every edit, times five images.
#
# The one-off cluster/registry setup this expects is NOT created automatically —
# see "Nested clusters (k3d / kind)" in docs/local-development.md.
_k3d = k8s_context().startswith('k3d-')
_kind = k8s_context() == 'kind-kind'
if _k3d:
    # Tilt pushes to localhost:5111 from the host; the cluster pulls the
    # rewritten name, which is how the NODE resolves the same registry.
    default_registry(
        'localhost:5111',
        host_from_cluster='k3d-tp-registry:5111',
    )

# Opt-in horizontal-scale profile (#1056): `tilt up -- --scale` layers
# helm/terrapod/values-scale.yaml on top of the dev overrides — API/web/listener
# HPAs, a sized embedded Postgres + Redis, and multi-replica-on-filesystem
# (single-node only). Used for the local load test; see docs/scalability.md.
config.define_bool('scale')
_scale = config.parse().get('scale', False)
if _scale:
    warn('SCALE profile ON: values-scale.yaml layered — HA/autoscaling for load testing.')

# Tilt evaluates this file during `down` too; teardown must not run setup work.
# Ensure namespace exists
if config.tilt_subcommand != 'down':
    local('kubectl create namespace terrapod --dry-run=client -o yaml | kubectl apply -f -')

# ─────────────────────────────────────────────────────────────────────────────
# Local TLS Certificates (mkcert)
# ─────────────────────────────────────────────────────────────────────────────

local_resource(
    'setup-certificates',
    cmd='''
CERT_DIR="$HOME/.local/share/terrapod/certs"
NAMESPACE="terrapod"

if command -v mkcert &> /dev/null; then
    mkcert -install 2>/dev/null
elif [ ! -s "$CERT_DIR/cert.pem" ] || [ ! -s "$CERT_DIR/key.pem" ]; then
    echo "ERROR: mkcert is required to generate local TLS certificates."
    exit 1
fi

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

# terrapod-query (#823) is baked into the API and runner images from a pre-built
# linux binary staged at docker/query-bin/. In CI the build-query job produces
# it; locally we compile it in a golang container (no host Go toolchain needed).
# The container is linux/<host arch>, matching the local k8s nodes. This runs at
# Tiltfile eval so the binary exists before the image builds; the runner build
# reruns it (below) so query source edits are picked up without a Tilt restart.
QUERY_BUILD = (
    'mkdir -p docker/query-bin && ' +
    'docker run --rm -v "$PWD/query":/src:ro -v "$PWD/docker/query-bin":/out ' +
    '-w /src golang:1.26.10 go build -trimpath -o /out/terrapod-query ./cmd/terrapod-query'
)
if config.tilt_subcommand != 'down':
    local(QUERY_BUILD, quiet=True)

# The agent is copied from the runner image into execution environments, so
# build it without a libc dependency before building the runner image.
EE_AGENT_BUILD = (
    'mkdir -p docker/ee-agent-bin && ' +
    'docker run --rm -e CGO_ENABLED=0 -v "$PWD/ee-agent":/src:ro ' +
    '-v "$PWD/docker/ee-agent-bin":/out -w /src golang:1.26.10 ' +
    'go build -trimpath -o /out/terrapod-ee-agent .'
)
if config.tilt_subcommand != 'down':
    local(EE_AGENT_BUILD, quiet=True)

# API Server (Python)
docker_build(
    'terrapod-api',
    context='.',
    dockerfile='docker/Dockerfile.api',
    # podman's build endpoint rejects a TAGGED reference in `cachefrom`
    # ("repository must contain neither a tag nor digest") and Tilt passes
    # it verbatim, so the hint is dropped there. Docker keeps it.
    cache_from=[] if _k3d else ['terrapod-api:latest'],
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
        sync('./services/terrapod/engines', '/app/terrapod/engines'),
        sync('./services/terrapod/runner', '/app/terrapod/runner'),
    ],
)

# Migrations (separate image — just SQLAlchemy + asyncpg + alembic)
docker_build(
    'terrapod-migrations',
    context='.',
    dockerfile='docker/Dockerfile.migrations',
)

# Runner Job image (python:3.14-slim, pure-Python orchestrator).
# Built as a local_resource (not docker_build) because the runner image is
# referenced in the runners.yaml ConfigMap, not in a pod spec — so Tilt does not
# track it and `default_registry` above does NOT apply to it. It therefore has to
# deliver itself, or runner Jobs fail ErrImageNeverPull: nested clusters have
# separate image stores, and nothing crosses between them by itself.
#
# The helm `set` block at the bottom of this file points the runner image at
# k3d-tp-registry:5111, which is how the NODE resolves the registry; the push
# below targets localhost:5111, which is how the HOST reaches the same thing.
# values-local.yaml is left alone on purpose — it is shared, and must not
# hard-code any one workstation's registry.
_RUNNER_TAG = 'localhost:5111/terrapod-runner:local' if _k3d else 'terrapod-runner:local'
_RUNNER_BUILD = ' && docker build -f docker/Dockerfile.runner -t ' + _RUNNER_TAG + ' .'
if _k3d:
    _RUNNER_BUILD = _RUNNER_BUILD + ' && docker push ' + _RUNNER_TAG
elif _kind:
    _RUNNER_BUILD = _RUNNER_BUILD + ' && kind load docker-image ' + _RUNNER_TAG + ' --name kind'

local_resource(
    'build-runner-image',
    # The runner is referenced from runners.yaml rather than a pod spec, so
    # Tilt cannot deliver it automatically. Push on k3d, import on kind; other
    # local clusters retain the existing plain build.
    cmd=QUERY_BUILD + ' && ' + EE_AGENT_BUILD + _RUNNER_BUILD,
    deps=[
        'docker/Dockerfile.runner',
        'query/cmd',
        'query/internal',
        'ee-agent',
        # Both: the requirements file is what the image installs, so it is the
        # one that changes the image, and without it a regenerated lock would
        # not rebuild here (GHSA-rgvw-c74c-h75w). The manifest stays because it
        # is where a floor is edited before regenerating, and rebuilding on that
        # edit is harmless.
        'services/pyproject-runner.toml',
        'services/requirements-runner.txt',
        'services/terrapod/http_retry.py',
        'services/terrapod/runner/__init__.py',
        'services/terrapod/runner/runner_config.py',
        'services/terrapod/runner/download.py',
        'services/terrapod/runner/reserved_env.py',
        'services/terrapod/runner/exec_subprocess.py',
        'services/terrapod/runner/ee_client.py',
        'services/terrapod/runner/debug_linger.py',
        'services/terrapod/runner/lock_extender.py',
        'services/terrapod/runner/plan_artifacts.py',
        'services/terrapod/runner/job_entrypoint.py',
        'services/terrapod/runner/phases',
        'services/terrapod/runner/saas_known_hosts',
        # The ansible callback plugins (#1928, #2144). They are DATA the
        # Dockerfile COPYs to a destination outside `terrapod/`, so the
        # `phases` entry above does not cover them -- and without this line
        # editing a plugin triggers no rebuild, so the pod keeps running the
        # stale one while the source looks fixed. The runner image has no
        # live_update, so a rebuild is the only loop there is.
        'services/terrapod/runner/ansible_callback',
        'services/terrapod/services/cost',
        'services/terrapod/services/inventory_render.py',
        'services/terrapod/services/ansible_auth.py',
    ],
    labels=['build'],
)

# Build and sign the local provider plugin. Registry staging and runner-side
# installation are separate; watching .dev-build would rebuild in a loop.
_PROVIDER_TAG = os.getenv('TERRAPOD_DEV_PROVIDER_TAG', '')
if not _PROVIDER_TAG:
    _PROVIDER_TAGS = str(local("git tag --list 'v[0-9]*' --sort=-version:refname")).strip()
    if not _PROVIDER_TAGS:
        fail('No local version tags found; fetch tags or set TERRAPOD_DEV_PROVIDER_TAG')
    _PROVIDER_TAG = _PROVIDER_TAGS.split('\n')[0]
if not _PROVIDER_TAG.startswith('v'):
    fail('TERRAPOD_DEV_PROVIDER_TAG must be a version tag such as v1.9.0')
watch_file('provider/GNUmakefile')
watch_file('provider/dev-package.sh')
watch_file('provider/main.go')
watch_file('provider/internal')
watch_file('go-terrapod')
_PROVIDER_VERSION = str(local('bash provider/dev-package.sh version ' + shlex.quote(_PROVIDER_TAG[1:]))).strip()
# Helm must render the SAME completed artifact version; local_resource builds run
# after rendering, so prime the package here before declaring the API Deployment.
if config.tilt_subcommand != 'down':
    local('make -C provider dev-package BASE_VERSION=' + shlex.quote(_PROVIDER_TAG[1:]), quiet=True)
local_resource(
    'build-provider-binary',
    cmd='make -C provider dev-package BASE_VERSION=' + shlex.quote(_PROVIDER_TAG[1:]),
    deps=[
        'provider/GNUmakefile',
        'provider/dev-package.sh',
        'provider/go.mod',
        'provider/go.sum',
        'provider/main.go',
        'provider/internal',
        'go-terrapod',
        'provider/examples/modules/terrapod_self_contained/main.tf',
    ],
    labels=['build'],
)

# Web UI (Next.js).
#
# Default dev loop: build the `builder` stage and run `next dev` for hot reload.
# Under `--scale` (the load-test profile) that dev server is NOT representative —
# `next dev` is a slow, single-process JIT compiler that bottlenecks the BFF on
# latency, not CPU, and taints any frontend/BFF scalability measurement. So scale
# mode builds the PRODUCTION image instead (the final Dockerfile.web stage:
# `npm run build` standalone output, `NODE_ENV=production`, `node server.js`) —
# the same frontend that ships — with no entrypoint override and no live_update.
if _scale:
    docker_build(
        'terrapod-web',
        context='.',
        dockerfile='docker/Dockerfile.web',
    )
else:
    docker_build(
        'terrapod-web',
        context='.',
        dockerfile='docker/Dockerfile.web',
        target='builder',
        entrypoint=['npx', 'next', 'dev', '-H', '0.0.0.0'],
        live_update=[
            sync('./web/src', '/app/src'),
            sync('./web/public', '/app/public'),
            sync('./web/messages', '/app/messages'),
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
# SSH Target Fixture (#2318)
# ─────────────────────────────────────────────────────────────────────────────
# A real managed node to configure, because every playbook exercise in this
# repository was `connection: local` and the only `connection: ssh` host in the
# tree is engineered to be unreachable -- so SSH, the machine credential and
# host-key verification had never been executed in the success direction.
#
# Loaded by its OWN k8s_yaml rather than added to the chart. A chart template
# would pull a dev fixture into `values.schema.json`, the three-profile config
# contract and `values-eval.yaml`'s zero-external-deps promise; an imperative
# `kubectl apply` would survive `tilt down` and never appear in the UI. This way
# it has a real Tilt lifecycle and the chart is untouched.
#
# The keypair is GENERATED and never committed -- a private key in a public
# repository is a secret-scanner finding whatever its intended lifetime. Same
# shape as setup-certificates above: idempotent, kept in the operator's own data
# directory, pushed in as a Secret.

local_resource(
    'setup-ssh-target-key',
    cmd='''
KEY_DIR="$HOME/.local/share/terrapod/ssh-target"
NAMESPACE="terrapod"

mkdir -p "$KEY_DIR"

if [ ! -f "$KEY_DIR/id_ed25519" ]; then
    echo "Generating an SSH keypair for the configure fixture..."
    ssh-keygen -q -t ed25519 -N '' -f "$KEY_DIR/id_ed25519" -C "terrapod-ssh-target"
fi

kubectl create secret generic ssh-target-authorized-keys \
    --from-file=target="$KEY_DIR/id_ed25519.pub" \
    --namespace="$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "The fixture accepts this key. To drive a configure against it, create a"
echo "machine credential whose private_key is the contents of:"
echo "    $KEY_DIR/id_ed25519"
echo "and point an inventory host at host 'ssh-target' as user 'target'."
''',
    labels=['setup'],
)

docker_build(
    'terrapod-ssh-target',
    context='docker',
    dockerfile='docker/Dockerfile.ssh-target',
    only=['Dockerfile.ssh-target'],
)

watch_file('dev/ssh-target.yaml')
k8s_yaml('dev/ssh-target.yaml')

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

_values = ['helm/terrapod/values.yaml', 'helm/terrapod/values-local.yaml']
if _scale:
    watch_file('helm/terrapod/values-scale.yaml')
    _values.append('helm/terrapod/values-scale.yaml')

k8s_yaml(helm(
    'helm/terrapod',
    name='terrapod',
    namespace='terrapod',
    values=_values,
    set=[
        'api.devProviderVersion=' + _PROVIDER_VERSION,
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
    ] + ([
        # The registry as the NODE resolves it. Set here rather than in
        # values-local.yaml, which is shared and must not hard-code one
        # workstation's registry.
        'runners.image.repository=k3d-tp-registry:5111/terrapod-runner',
        # ...and the policy has to permit the pull that repository implies.
        # values-local.yaml says Never, which is right everywhere the image is
        # built straight into the node's store. On k3d it is *pushed to a
        # registry* instead (podman and the node keep separate stores), so Never
        # forbids the only route the image has: the Job fails ErrImageNeverPull
        # against an image that was built and pushed perfectly well.
        #
        # Always, not IfNotPresent: the tag is the mutable `:local`, rebuilt on
        # every change to the runner sources. IfNotPresent would pull once and
        # then serve that first image forever, so every subsequent edit would
        # appear not to take — the same stale-image confusion as the layer cache,
        # arriving through a different door. The registry is in-cluster, so the
        # re-pull costs nothing worth saving.
        'runners.image.pullPolicy=Always',
    ] if _k3d else []),
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

local_resource(
    'seed-dev-provider-cache',
    cmd='bash scripts/dev-provider-stage.sh ' + shlex.quote(_PROVIDER_VERSION),
    deps=['scripts/dev-provider-stage.sh'],
    # Tilt groups the chart's PVC into uncategorized. Wait for that YAML to be
    # applied before the helper pod tries to mount terrapod-storage.
    resource_deps=['build-provider-binary', 'uncategorized'],
    labels=['setup'],
)

# API Server (accessed via Ingress at https://terrapod.local)
k8s_resource(
    'terrapod-api',
    labels=['backend'],
    resource_deps=['terrapod-postgresql', 'terrapod-migrations-1', 'setup-certificates', 'seed-dev-provider-cache'],
)

# The SSH target fixture (#2318). Depends on the keypair resource, because the
# Deployment mounts the Secret that resource creates -- without the ordering the
# pod sits in ContainerCreating on a missing Secret, which reads as a broken
# fixture rather than a race.
k8s_resource(
    'ssh-target',
    labels=['dev-fixture'],
    resource_deps=['setup-ssh-target-key'],
)

# Web UI (accessed via Ingress at https://terrapod.local)
k8s_resource(
    'terrapod-web',
    labels=['frontend'],
    resource_deps=['terrapod-api'],
    links=[link('https://terrapod.local', 'Terrapod')],
)

# Runner Listener (uses same image as API). The agent pool + join token are
# created by the chart's bootstrap Job (terrapod-bootstrap-1), driven by
# `bootstrap.poolName` / `bootstrap.poolToken` in values-local.yaml — the SAME
# mechanism the kind/k3d eval profile uses (values-eval.yaml). The listener joins
# that pool with the matching `listener.joinToken`, so it depends on the bootstrap
# Job (pool must exist) and the API (the join target). No separate pool-setup step.
k8s_resource(
    'terrapod-listener',
    labels=['backend'],
    resource_deps=['terrapod-bootstrap-1', 'terrapod-api', 'terrapod-web'],
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
