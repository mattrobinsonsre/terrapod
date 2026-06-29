#!/usr/bin/env bash
# Config-channel contract (#617): the chart ↔ code ↔ chart-test contract.
#
# Renders the Helm chart (every values profile) and parses the rendered
# ConfigMaps THROUGH the real Pydantic config models (Settings / RunnerConfig),
# asserting:
#   - no drift: every key the chart renders is a real field on the model;
#   - coverage: contract-spine keys (rate_limit, registry.module_interface,
#     the migrated listener settings) are present;
#   - env channel: rendered Deployments carry no non-sensitive TERRAPOD_* env.
#
# All in Docker — no local Python/helm needed. Run locally with
# `scripts/helm-config-contract.sh` or `make helm-config-contract`; CI runs it
# in the `helm-config-contract` job. The checker logic is
# scripts/check_helm_config_contract.py.
set -euo pipefail
cd "$(dirname "$0")/.."

HELM_IMAGE="alpine/helm:3.17.2"
PY_IMAGE="python:3.13-slim"
RENDER_DIR=".helmrender"
PROFILES=(values.yaml values-local.yaml values-eval.yaml)

rm -rf "$RENDER_DIR"
mkdir -p "$RENDER_DIR"
trap 'rm -rf "$RENDER_DIR"' EXIT

echo "==> Rendering chart for each values profile"
for f in "${PROFILES[@]}"; do
  docker run --rm -v "$PWD:/chart" -w /chart "$HELM_IMAGE" \
    template terrapod helm/terrapod -f "helm/terrapod/$f" > "$RENDER_DIR/$f"
done

echo "==> Parsing rendered ConfigMaps through the real config models"
# Pydantic v2 + pyyaml only; the models read `.model_fields` (no instantiation,
# so no secrets/env needed). PYTHONPATH=services makes `terrapod.config`
# importable (PEP 420 namespace package).
docker run --rm -v "$PWD:/w" -w /w "$PY_IMAGE" sh -c '
  pip install -q "pydantic>=2" "pydantic-settings>=2" pyyaml
  fail=0
  for f in '"${PROFILES[*]}"'; do
    PYTHONPATH=services python scripts/check_helm_config_contract.py ".helmrender/$f" "$f" || fail=1
  done
  exit $fail
'
