#!/usr/bin/env bash
# Drive a real plan through the eval stack's runner listener and assert it
# reaches `planned`. This proves the WHOLE server-side-execution path end to
# end: the listener claims the run, launches a Kubernetes Job, the runner Job
# downloads the config from the in-cluster filesystem storage (api base_url),
# runs `tofu plan`, and reports back. A listener that merely joined a pool
# wouldn't prove any of that.
#
# Usage: scripts/eval-smoke-plan.sh <kube-context> [namespace]
#   Defaults: namespace=terrapod-eval, API port-forward on :18000.
set -euo pipefail

CTX="${1:?usage: eval-smoke-plan.sh <kube-context> [namespace]}"
NS="${2:-terrapod-eval}"
PORT="${TERRAPOD_SMOKE_API_PORT:-18000}"
API="http://localhost:${PORT}"
DEADLINE="${TERRAPOD_SMOKE_DEADLINE_SECONDS:-360}"  # runner pulls image + tofu binary on first run

log() { echo "==> $*"; }
die() { echo "SMOKE FAIL: $*" >&2; exit 1; }

k() { kubectl --context "$CTX" -n "$NS" "$@"; }

# ── 1. Mint an admin API token directly in the API pod ────────────────────────
log "Minting an admin API token in the API pod…"
TOKEN=$(k exec deploy/terrapod-api -- python3 -c "
import asyncio
from terrapod.db.session import init_db, get_db_session
from terrapod.auth import api_tokens
async def main():
    await init_db()
    async with get_db_session() as db:
        _, raw = await api_tokens.create_api_token(
            db, bound_to='admin', created_by='admin', kind='interactive', lifespan_hours=2)
        await db.commit()
        import sys; sys.stderr.write('TOKEN:'+raw+'\n')
asyncio.run(main())
" 2>&1 | grep -oE 'TOKEN:[^[:space:]]+' | sed 's/TOKEN://' | tr -d '\r')
[ -n "$TOKEN" ] || die "could not mint API token"

# ── 2. Resolve the bootstrapped eval-pool id from the DB (deterministic) ──────
POOL_UUID=$(k exec deploy/terrapod-api -- python3 -c "
import asyncio
from sqlalchemy import select
from terrapod.db.session import init_db, get_db_session
from terrapod.db import models
async def main():
    await init_db()
    async with get_db_session() as db:
        p=(await db.execute(select(models.AgentPool).where(models.AgentPool.name=='eval-pool'))).scalars().first()
        import sys; sys.stderr.write('POOL:'+(str(p.id) if p else '')+'\n')
asyncio.run(main())
" 2>&1 | grep -oE 'POOL:[0-9a-f-]+' | sed 's/POOL://' | tr -d '\r')
[ -n "$POOL_UUID" ] || die "eval-pool not found (bootstrap pool missing)"
log "eval-pool id: ${POOL_UUID}"

# ── 3. Port-forward the API ───────────────────────────────────────────────────
k port-forward svc/terrapod-api "${PORT}:8000" >/tmp/eval-smoke-pf.log 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
sleep 4

auth=(-H "Authorization: Bearer ${TOKEN}")
jsonapi=(-H "Content-Type: application/vnd.api+json")

# ── 4. Create an agent-mode workspace bound to the eval-pool ──────────────────
WSNAME="eval-smoke-$$"
log "Creating agent workspace ${WSNAME}…"
WS=$(curl -fsS -X POST "${API}/api/v2/organizations/default/workspaces" "${auth[@]}" "${jsonapi[@]}" \
  -d "{\"data\":{\"type\":\"workspaces\",\"attributes\":{\"name\":\"${WSNAME}\",\"execution-mode\":\"agent\",\"agent-pool-id\":\"apool-${POOL_UUID}\",\"auto-apply\":false}}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")
[ -n "$WS" ] || die "workspace create failed"

# ── 5. Upload a trivial config (output only — no providers to download) ───────
TMPD=$(mktemp -d)
printf 'output "ok" {\n  value = "eval-listener-works"\n}\n' > "${TMPD}/main.tf"
tar -C "$TMPD" -czf "${TMPD}/cfg.tar.gz" main.tf
log "Creating + uploading configuration version…"
UPURL=$(curl -fsS -X POST "${API}/api/v2/workspaces/${WS}/configuration-versions" "${auth[@]}" "${jsonapi[@]}" \
  -d '{"data":{"type":"configuration-versions","attributes":{"auto-queue-runs":false}}}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['attributes']['upload-url'])")
UPURL=$(echo "$UPURL" | sed -E "s#https?://[^/]+#${API}#")
curl -fsS -o /dev/null -X PUT "$UPURL" --data-binary @"${TMPD}/cfg.tar.gz" -H "Content-Type: application/octet-stream"

# ── 6. Queue a plan-only run ──────────────────────────────────────────────────
log "Queuing a plan-only run…"
RUN=$(curl -fsS -X POST "${API}/api/v2/runs" "${auth[@]}" "${jsonapi[@]}" \
  -d "{\"data\":{\"type\":\"runs\",\"attributes\":{\"plan-only\":true,\"message\":\"eval listener smoke\"},\"relationships\":{\"workspace\":{\"data\":{\"type\":\"workspaces\",\"id\":\"${WS}\"}}}}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")
[ -n "$RUN" ] || die "run create failed"
log "run ${RUN} — waiting up to ${DEADLINE}s for it to plan on a Job…"

# ── 7. Poll to a terminal status ──────────────────────────────────────────────
elapsed=0
status=""
while [ "$elapsed" -lt "$DEADLINE" ]; do
  status=$(curl -fsS "${API}/api/v2/runs/${RUN}" "${auth[@]}" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['attributes'].get('status',''))" 2>/dev/null || echo "")
  jobs=$(k get pods -l app.kubernetes.io/name=terrapod-runner 2>/dev/null | grep -c tprun || true)
  echo "  [${elapsed}s] status=${status} runner-job-pods=${jobs:-0}"
  case "$status" in
    planned|planned_and_finished) log "PLAN SUCCEEDED through the listener (status=${status})"; exit 0 ;;
    errored|canceled) die "run reached ${status} — server-side execution failed (check runner Job logs)" ;;
  esac
  sleep 10; elapsed=$((elapsed + 10))
done
k get pods | grep -E "tprun" || true
die "run did not reach 'planned' within ${DEADLINE}s (last status=${status:-none})"
