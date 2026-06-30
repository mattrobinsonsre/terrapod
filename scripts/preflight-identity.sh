#!/usr/bin/env bash
# Run the cloud-identity preflight doctor (#571) against an EXISTING Terrapod
# release in the current kube context — verifies the API + runner ServiceAccounts
# can assume their cloud role and read the object store, BEFORE a real run fails.
#
# Usage:
#   make preflight-identity                      # release=terrapod ns=terrapod
#   RELEASE=tp NAMESPACE=infra make preflight-identity
#   VALUES=helm/terrapod/values-prod.yaml make preflight-identity
#
# It renders only the preflight Jobs (with preflight.enabled=true) from the same
# chart, applies them to the running namespace so they run under the real SAs,
# waits, and streams the pass/fail report. This is the on-demand counterpart to
# the opt-in Helm hook (`preflight.enabled: true` in values).
set -euo pipefail
cd "$(dirname "$0")/.."

RELEASE="${RELEASE:-terrapod}"
NAMESPACE="${NAMESPACE:-terrapod}"
HELM_IMAGE="alpine/helm:3.17.2"

extra=()
[ -n "${VALUES:-}" ] && extra+=(-f "$VALUES")

echo "==> Rendering preflight Jobs for release '$RELEASE' in namespace '$NAMESPACE'"
manifest="$(docker run --rm -v "$PWD:/chart" -w /chart "$HELM_IMAGE" \
  template "$RELEASE" helm/terrapod \
    --namespace "$NAMESPACE" \
    "${extra[@]}" \
    --set preflight.enabled=true \
    --show-only templates/job-preflight.yaml)"

if [ -z "$manifest" ]; then
  echo "FAIL: nothing rendered — check RELEASE/VALUES" >&2
  exit 1
fi

# Clear any prior preflight Jobs (fixed names → re-run safe), then apply.
kubectl delete job -n "$NAMESPACE" -l app.kubernetes.io/component=preflight --ignore-not-found
echo "$manifest" | kubectl apply -n "$NAMESPACE" -f -

echo "==> Waiting for preflight Jobs to complete…"
rc=0
kubectl wait -n "$NAMESPACE" --for=condition=complete --timeout=180s \
  job -l app.kubernetes.io/component=preflight || rc=$?

echo "==> Preflight output:"
kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/component=preflight --tail=-1 --prefix || true

if [ "$rc" -ne 0 ]; then
  echo "==> Preflight did NOT pass (see output above + docs/cloud-credentials.md#troubleshooting)" >&2
  exit 1
fi
echo "==> Preflight PASSED"
