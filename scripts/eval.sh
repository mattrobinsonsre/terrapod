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
ADMIN_EMAIL="admin"
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

# ── Helm version guard ────────────────────────────────────────────────────────
# helm 4.2.1 shipped "prevent spurious early exit in WaitForDelete during
# informer sync" (helm/helm#32081) and reverted it in 4.2.2. While it was in,
# a hook whose `before-hook-creation` delete finds nothing — i.e. EVERY hook on
# a fresh install — waits for a full informer sync instead of returning
# immediately. That is roughly nine minutes each, and this chart has several, so
# `make eval` sits silent for the best part of an hour and helm's own --timeout
# does not bound it.
#
# It is worth a hard stop rather than a warning: the symptom is indistinguishable
# from a hang, and the first thing anyone tries is to kill it and start again.
BAD_HELM_VERSIONS="4.2.1"
check_helm_version() {
  local v
  v="$(helm version --template '{{.Version}}' 2>/dev/null | sed 's/^v//; s/+.*//')" || return 0
  [ -n "$v" ] || return 0
  case " $BAD_HELM_VERSIONS " in
    *" $v "*)
      die "helm ${v} cannot install this chart in reasonable time.

  It waits ~9 minutes per hook on a fresh install (helm/helm#32081, reverted in
  4.2.2), so this looks like a hang. Upgrade helm and re-run:

      brew upgrade helm     # or: https://helm.sh/docs/intro/install/

  Any helm other than ${v} is fine." ;;
  esac
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
  check_helm_version
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
  # Optional image-registry override. When set (e.g. CI's k3d leg points every
  # image at a k3d-managed local registry, which is a deterministic pull instead
  # of the flaky `k3d image import`), redirect all five images via
  # global.imageRegistry. Empty by default, so a normal `make eval` — which loads
  # images straight into the node — is unaffected.
  local reg_args=()
  if [[ -n "${TERRAPOD_EVAL_IMAGE_REGISTRY:-}" ]]; then
    reg_args+=(--set "global.imageRegistry=${TERRAPOD_EVAL_IMAGE_REGISTRY}")
  fi
  # `--wait` blocks until every Deployment — including the runner listener — is
  # ready. The listener only reports ready once it has JOINED the agent pool, and
  # the pool is created by the bootstrap job, which is a PRE-install hook (see
  # job-bootstrap.yaml) so it exists before the listener starts. That ordering is
  # what makes `--wait` safe here: a working server-side-runs stack the moment the
  # install returns, not just pods that exist.
  # Run helm in the background and report what the cluster is doing while it
  # works. `helm --wait` prints nothing at all until it returns, so a slow pull
  # and a genuine hang look identical — and the first one of those is normal on a
  # first run, which trains people to wait through the second. A status line
  # every 20s costs nothing and turns "it is stuck" into "it is pulling images".
  helm --kube-context "$ctx" upgrade --install "$RELEASE" "$CHART_DIR" \
    --namespace "$NS" --create-namespace \
    -f "${CHART_DIR}/values-eval.yaml" \
    "${reg_args[@]}" \
    --set "api.image.tag=${VERSION}" \
    --set "web.image.tag=${VERSION}" \
    --set "migrations.image.tag=${VERSION}" \
    --set "listener.image.tag=${VERSION}" \
    --set "runners.image.tag=${VERSION}" \
    --set "bootstrap.adminEmail=${ADMIN_EMAIL}" \
    --set "bootstrap.adminPassword=${ADMIN_PASSWORD}" \
    --set "api.config.external_url=http://localhost:${PF_PORT}" \
    --wait --timeout "${TERRAPOD_EVAL_HELM_TIMEOUT:-600s}" &
  local helm_pid=$!

  # An outer bound, because helm's own --timeout does not always hold it: the
  # 4.2.1 delete-wait above ran well past 600s. Whatever goes wrong, this command
  # ends and says something rather than sitting there.
  local budget="${TERRAPOD_EVAL_WATCHDOG:-1200}" waited=0 step=20
  while kill -0 "$helm_pid" 2>/dev/null; do
    sleep "$step"; waited=$((waited + step))
    # helm may have finished during that sleep; without this the loop prints one
    # last status line on top of the success banner.
    kill -0 "$helm_pid" 2>/dev/null || break
    if [ "$waited" -ge "$budget" ]; then
      warn "helm has been running for ${waited}s with no result — giving up."
      kill "$helm_pid" 2>/dev/null || true
      kubectl --context "$ctx" -n "$NS" get pods || true
      die "install exceeded ${budget}s. Re-run with TERRAPOD_EVAL_WATCHDOG=<seconds> to allow longer, or 'scripts/eval.sh down' to clean up."
    fi
    # Pods first; before any exist, say which hook helm is on, so the silent
    # early phase is legible too.
    local pods
    pods="$(kubectl --context "$ctx" -n "$NS" get pods --no-headers 2>/dev/null || true)"
    if [ -n "$pods" ]; then
      echo "   … ${waited}s — $(echo "$pods" | awk '$3=="Running"||$3=="Completed"' | wc -l | tr -d ' ')/$(echo "$pods" | wc -l | tr -d ' ') pods ready: $(echo "$pods" | awk '{printf "%s(%s) ", $1, $3}')"
    else
      echo "   … ${waited}s — no pods yet (helm is still applying; images pull on first run)"
    fi
  done

  wait "$helm_pid" || {
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

  Server-side runs are enabled: a runner listener has joined the '${c_bold}eval-pool${c_reset}'
  agent pool, so you can create a workspace and queue a plan in the UI and it
  executes on a Kubernetes Job (first run pulls the runner image + a tofu binary).

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
