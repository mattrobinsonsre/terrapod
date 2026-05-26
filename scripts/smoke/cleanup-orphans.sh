#!/usr/bin/env bash
# Sweep Terrapod-side orphans left by a failed smoke run.
#
# The smoke fixtures stamp `test = "true"` and a per-run `smoke = "<id>"`
# label on every resource. Most resources can be torn down through
# the API; this script lists everything matching the label and
# DELETEs them. Idempotent — running twice doesn't error on already-
# gone resources.
#
# Usage:
#   scripts/smoke/cleanup-orphans.sh           # delete everything labelled test=true
#   SMOKE_ID=smoke-1234 scripts/smoke/cleanup-orphans.sh   # only that run

set -euo pipefail

TARGET=${TARGET:-https://terrapod.local}
TOKEN=${TERRAPOD_TOKEN:-${TOKEN:-}}
SMOKE_ID=${SMOKE_ID:-}
SKIP_TLS=${SKIP_TLS:-1}

if [ -z "$TOKEN" ] && [ -f "$HOME/.terraform.d/credentials.tfrc.json" ]; then
  # Match the exact host (strip scheme + trailing slash) — the file
  # may contain credentials for multiple terrapod instances and
  # picking the first "terrapod*" entry blindly grabs the wrong one.
  HOST_KEY=$(echo "${TARGET:-https://terrapod.local}" | gsed -E 's,^https?://,,; s,/$,,')
  TOKEN=$(python3 -c "
import json, sys, os
host = os.environ.get('HOST_KEY', 'terrapod.local')
with open(os.path.expanduser('~/.terraform.d/credentials.tfrc.json')) as f:
    d = json.load(f)
print(d.get('credentials', {}).get(host, {}).get('token', ''))
" HOST_KEY="$HOST_KEY" || true)
fi
if [ -z "$TOKEN" ]; then
  echo "FATAL: no Terrapod token" >&2
  exit 1
fi

CURL_FLAGS=(-s -H "Authorization: Bearer $TOKEN")
[ "$SKIP_TLS" = "1" ] && CURL_FLAGS+=(-k)

filter() {
  # Filter resource list to ones whose labels match (test=true,
  # optionally smoke=<SMOKE_ID>). The list endpoints return JSON:API
  # so jq does the matching.
  if [ -n "$SMOKE_ID" ]; then
    jq -r --arg id "$SMOKE_ID" '.data[] | select(.attributes.labels.smoke == $id) | .id'
  else
    jq -r '.data[] | select(.attributes.labels.test == "true") | .id'
  fi
}

delete_each() {
  local endpoint=$1
  local label=$2
  local ids
  ids=$(curl "${CURL_FLAGS[@]}" "$TARGET/api/terrapod/v1/$endpoint" | filter || true)
  if [ -z "$ids" ]; then
    echo "  no $label orphans"
    return
  fi
  while IFS= read -r id; do
    code=$(curl "${CURL_FLAGS[@]}" -o /dev/null -w "%{http_code}" -X DELETE "$TARGET/api/terrapod/v1/$endpoint/$id" || echo failed)
    echo "  DELETE $endpoint/$id → $code"
  done <<<"$ids"
}

echo "== Sweeping orphans on $TARGET"
if [ -n "$SMOKE_ID" ]; then
  echo "   scope: smoke = $SMOKE_ID"
else
  echo "   scope: test = true (all-time)"
fi

# Order matters — children before parents. Workspace removal cascades
# to its variables; varset removal cascades to varset-vars and
# workspace assignments.
delete_each "workspaces"                       "workspaces"
delete_each "varsets"                          "variable sets"
delete_each "agent-pools"                      "agent pools"
delete_each "roles"                            "roles"
delete_each "gpg-keys"                         "gpg keys"

echo "Done."
