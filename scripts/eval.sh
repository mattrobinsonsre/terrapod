#!/usr/bin/env bash
# Terrapod local evaluation quickstart.
#
#   scripts/eval.sh up      Create a throwaway kind/k3d cluster + install Terrapod
#                           (in-cluster Postgres/Redis, filesystem storage, local
#                           admin) and wait until it's ready. Prints the URL + creds.
#   scripts/eval.sh down    Delete the eval cluster.
#   scripts/eval.sh status  Show pod status.
#
# Auto-detects `kind` (preferred) or `k3d`; pin with TERRAPOD_EVAL_TOOL=kind|k3d
# when both are installed. Uses released images (tag overridable via
# TERRAPOD_VERSION, default `latest`). NOT for production — see values-eval.yaml.
set -euo pipefail

CLUSTER="${TERRAPOD_EVAL_CLUSTER:-terrapod-eval}"
# Distinct namespace + a throwaway cluster keep the eval fully isolated from any
# Tilt-deployed Terrapod on your default cluster (which uses the `terrapod` ns).
NS="${TERRAPOD_EVAL_NAMESPACE:-terrapod-eval}"
RELEASE="terrapod"
VERSION="${TERRAPOD_VERSION:-latest}"
PF_PORT="${TERRAPOD_EVAL_PORT:-8080}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART_DIR="${REPO_ROOT}/helm/terrapod"
ADMIN_EMAIL="admin@example.com"
ADMIN_PASSWORD="terrapod"

# Caller's kube-context, captured before we create a cluster (kind/k3d switch it).
# Script-global so the EXIT trap can restore it after up() returns; default empty
# keeps `set -u` happy when it was never set.
prev_ctx=""
restore_ctx() { [ -n "${prev_ctx:-}" ] && kubectl config use-context "$prev_ctx" >/dev/null 2>&1 || true; }

c_green=$'\033[0;32m'; c_bold=$'\033[1m'; c_yel=$'\033[0;33m'; c_reset=$'\033[0m'
log()  { echo "${c_green}==>${c_reset} $*"; }
warn() { echo "${c_yel}!! ${c_reset} $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# ── Cluster tool detection ────────────────────────────────────────────────────
# TERRAPOD_EVAL_TOOL pins the tool explicitly (kind|k3d); otherwise prefer kind.
# The override matters when both are installed and you need a specific one — e.g.
# CI's k3d matrix job runs on a runner that ALSO ships a pre-installed `kind`, so
# without the pin auto-detection would silently pick kind and the smoke step's
# k3d-pinned context lookup would miss.
detect_tool() {
  if [ -n "${TERRAPOD_EVAL_TOOL:-}" ]; then
    case "$TERRAPOD_EVAL_TOOL" in
      kind|k3d)
        command -v "$TERRAPOD_EVAL_TOOL" >/dev/null 2>&1 \
          || die "TERRAPOD_EVAL_TOOL=$TERRAPOD_EVAL_TOOL but '$TERRAPOD_EVAL_TOOL' is not installed"
        echo "$TERRAPOD_EVAL_TOOL" ;;
      *) die "TERRAPOD_EVAL_TOOL must be 'kind' or 'k3d', got '$TERRAPOD_EVAL_TOOL'" ;;
    esac
    return
  fi
  if command -v kind >/dev/null 2>&1; then echo kind
  elif command -v k3d >/dev/null 2>&1; then echo k3d
  else die "neither 'kind' nor 'k3d' found — install one: https://kind.sigs.k8s.io or https://k3d.io"; fi
}

cluster_exists() {
  case "$1" in
    kind) kind get clusters 2>/dev/null | grep -qx "$CLUSTER" ;;
    k3d)  k3d cluster list -o json 2>/dev/null | grep -q "\"name\":\"$CLUSTER\"" ;;
  esac
}

kube_context() {
  case "$1" in
    kind) echo "kind-${CLUSTER}" ;;
    k3d)  echo "k3d-${CLUSTER}" ;;
  esac
}

create_cluster() {
  local tool="$1"
  if cluster_exists "$tool"; then
    log "Reusing existing ${tool} cluster '${CLUSTER}'"
    return
  fi
  log "Creating ${tool} cluster '${CLUSTER}' (throwaway)…"
  case "$tool" in
    kind) kind create cluster --name "$CLUSTER" --wait 120s ;;
    k3d)  k3d cluster create "$CLUSTER" --wait --timeout 120s ;;
  esac
}

# ── Up ────────────────────────────────────────────────────────────────────────
up() {
  command -v helm >/dev/null 2>&1 || die "helm not found"
  command -v kubectl >/dev/null 2>&1 || die "kubectl not found"
  local tool ctx; tool="$(detect_tool)"; ctx="$(kube_context "$tool")"
  # Preserve the caller's current kubectl context — `kind`/`k3d` create switches
  # it to the new cluster, which would yank your default context (e.g. away from
  # a Tilt-deployed Terrapod). We pin every command below to --context "$ctx" and
  # restore the original on exit, so the eval never touches your active context.
  prev_ctx="$(kubectl config current-context 2>/dev/null || true)"
  trap restore_ctx EXIT
  create_cluster "$tool"
  restore_ctx

  log "Installing Terrapod (${RELEASE}) into namespace '${NS}' using image tag '${VERSION}'…"
  helm --kube-context "$ctx" upgrade --install "$RELEASE" "$CHART_DIR" \
    --namespace "$NS" --create-namespace \
    -f "${CHART_DIR}/values-eval.yaml" \
    --set "api.image.tag=${VERSION}" \
    --set "web.image.tag=${VERSION}" \
    --set "migrations.image.tag=${VERSION}" \
    --set "bootstrap.adminEmail=${ADMIN_EMAIL}" \
    --set "bootstrap.adminPassword=${ADMIN_PASSWORD}" \
    --set "api.config.external_url=http://localhost:${PF_PORT}" \
    --wait --timeout 300s || {
      warn "helm install did not report ready in time — showing pod status:"
      kubectl --context "$ctx" -n "$NS" get pods || true
      die "install failed (see pod status above; 'scripts/eval.sh status' to re-check)"
    }

  log "Waiting for the web frontend to be ready…"
  kubectl --context "$ctx" -n "$NS" rollout status deploy/${RELEASE}-web --timeout=180s

  print_banner "$ctx"
  if [[ -z "${CI:-}" && -z "${EVAL_NO_PORT_FORWARD:-}" ]]; then
    log "Starting port-forward (Ctrl-C to stop; the cluster keeps running — 'make eval-down' to delete)…"
    exec kubectl --context "$ctx" -n "$NS" port-forward "svc/${RELEASE}-web" "${PF_PORT}:3000"
  fi
}

print_banner() {
  local ctx="$1"
  cat <<EOF

${c_bold}${c_green}Terrapod is up.${c_reset}

  URL:       ${c_bold}http://localhost:${PF_PORT}${c_reset}   (after the port-forward below)
  Username:  ${c_bold}${ADMIN_EMAIL}${c_reset}
  Password:  ${c_bold}${ADMIN_PASSWORD}${c_reset}

  Port-forward (if not started automatically):
    kubectl --context ${ctx} -n ${NS} port-forward svc/${RELEASE}-web ${PF_PORT}:3000

  Tear down everything:
    make eval-down

${c_yel}Evaluation only${c_reset} — single-replica in-cluster Postgres/Redis, filesystem
storage, a known admin password. Not for production.
EOF
}

# ── Down / status ─────────────────────────────────────────────────────────────
down() {
  local tool; tool="$(detect_tool)"
  if cluster_exists "$tool"; then
    log "Deleting ${tool} cluster '${CLUSTER}'…"
    case "$tool" in
      kind) kind delete cluster --name "$CLUSTER" ;;
      k3d)  k3d cluster delete "$CLUSTER" ;;
    esac
  else
    log "No ${tool} cluster '${CLUSTER}' to delete."
  fi
}

status() {
  local tool ctx; tool="$(detect_tool)"; ctx="$(kube_context "$tool")"
  kubectl --context "$ctx" -n "$NS" get pods,svc
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) die "usage: $0 {up|down|status}" ;;
esac
