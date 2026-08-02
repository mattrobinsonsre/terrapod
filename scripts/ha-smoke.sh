#!/usr/bin/env zsh
# Two-node HA verification against the local cluster (#960, #1114).
#
# Both nodes are ordinary Helm releases in their own namespaces, each with its
# own Postgres and Redis — two nodes means two of everything, which is the point.
# The Tilt dev stack is deliberately untouched; this pair stands alone.
#
#   scripts/ha-smoke.sh up      deploy both nodes and the shared peer secrets
#   scripts/ha-smoke.sh verify  assert the pair actually converges
#   scripts/ha-smoke.sh reverse swap the roles and assert it works both ways
#   scripts/ha-smoke.sh table   walk the #960 release-gate table, honestly
#   scripts/ha-smoke.sh table 10   …or just one row, while working on it
#   scripts/ha-smoke.sh down    remove node B
#
# The peer link runs over in-cluster Service DNS. That is not a shortcut:
# `ha.peer.url` must be the peer's DIRECT per-node address, and a Service name is
# exactly that.
set -euo pipefail

REPO_ROOT="${0:a:h}/.."
CTX="${KUBE_CONTEXT:-rancher-desktop}"
NS_A="${NS_A:-terrapod-a}"
NS_B="${NS_B:-terrapod-b}"

k() { kubectl --context "$CTX" "$@"; }
api_a() { k -n "$NS_A" exec deploy/terrapod-a-api -- "$@"; }
api_b() { k -n "$NS_B" exec deploy/terrapod-b-api -- "$@"; }

fail() { print -P "%F{red}FAIL%f $*"; exit 1; }
ok()   { print -P "%F{green}ok%f   $*"; }
info() { print -P "%F{cyan}==>%f $*"; }

# One secret per direction, held by BOTH nodes — outbound on one side, inbound on
# the other. Generated once and reused across re-runs so `up` is idempotent.
ensure_secret() {
  local ns=$1 name=$2 value=$3
  k -n "$ns" create secret generic "$name" \
    --from-literal=client_secret="$value" \
    --dry-run=client -o yaml | k -n "$ns" apply -f - >/dev/null
}

cmd_up() {
  local ab ba
  ab=$(k -n "$NS_A" get secret terrapod-peer-a-to-b -o jsonpath='{.data.client_secret}' 2>/dev/null | base64 -d || true)
  ba=$(k -n "$NS_A" get secret terrapod-peer-b-to-a -o jsonpath='{.data.client_secret}' 2>/dev/null | base64 -d || true)
  [[ -n "$ab" ]] || ab=$(openssl rand -base64 32)
  [[ -n "$ba" ]] || ba=$(openssl rand -base64 32)

  for ns in "$NS_A" "$NS_B"; do
    k create namespace "$ns" --dry-run=client -o yaml | k apply -f - >/dev/null
  done
  for ns in "$NS_A" "$NS_B"; do
    ensure_secret "$ns" terrapod-peer-a-to-b "$ab"
    ensure_secret "$ns" terrapod-peer-b-to-a "$ba"
  done
  ok "peer secrets present in both namespaces"

  # Both nodes run what Tilt already built — no separate build, and it
  # guarantees the pair is the same code as the dev stack. The migrations job is
  # a SEPARATE image from the API: pinning only the API leaves the pre-install
  # hook pulling from GHCR, which on a local cluster never succeeds and shows up
  # as an inscrutable "Job in progress" timeout.
  #
  # But the pair does NOT run the `tilt-<hash>` tags directly. Those move: edit
  # a source file and Tilt rebuilds under a new hash, and the tag the pair
  # pinned can stop resolving — with `pullPolicy: Never` that is
  # `ErrImageNeverPull` on a pre-upgrade hook, i.e. a helm timeout minutes later
  # blaming a Job for something that is really a vanished tag. So the images are
  # re-tagged into a namespace the pair owns and Tilt never touches.
  local api_src mig_src
  # `|| true` is load-bearing. Under `set -e` a command substitution whose
  # pipeline exits non-zero kills the script AT THE ASSIGNMENT — so a missing
  # image aborted `up` silently, before the guard below could say which one,
  # printing nothing but the preceding "ok" line. Which is a miserable thing to
  # debug at midnight. Let the assignment succeed empty and let the guard talk.
  api_src=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^terrapod-api:tilt-' | head -1 || true)
  mig_src=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^terrapod-migrations:tilt-' | head -1 || true)
  # Name the one that is missing. "no tilt-built images" sends you looking at
  # the wrong thing when the API image is sitting right there and it is the
  # migrations image — a separate build — that got collected.
  [[ -n "$api_src" ]] \
    || fail "no terrapod-api:tilt-* image — run 'tilt up', or 'tilt trigger terrapod-api'"
  [[ -n "$mig_src" ]] \
    || fail "no terrapod-migrations:tilt-* image — run 'tilt trigger terrapod-migrations-1'"

  # The runner image is built by a Tilt `local_resource` under a fixed `:local`
  # tag rather than a content hash, so it does not move — but nothing references
  # it between runs either, which leaves it eligible for kubelet image GC. Same
  # symptom, so same treatment.
  local runner_src="terrapod-runner:local"
  docker image inspect "$runner_src" >/dev/null 2>&1 \
    || fail "no $runner_src — run 'tilt trigger build-runner-image' so the pair can execute runs"

  # Re-tag unless the caller has already staged its own build under :ha (which
  # is how a fix is tried on the pair before it exists in the dev stack).
  local api_image="terrapod-api:ha" mig_image="terrapod-migrations:ha"
  if [[ "${HA_KEEP_IMAGES:-}" != "1" ]]; then
    docker tag "$api_src" "$api_image"
    docker tag "$mig_src" "$mig_image"
    docker tag "$runner_src" "terrapod-runner:ha"
    info "pinned $api_src -> $api_image, $mig_src -> $mig_image, $runner_src -> terrapod-runner:ha"
  else
    info "HA_KEEP_IMAGES=1 — reusing whatever is tagged :ha"
  fi

  local node ns
  for node in a b; do
    [[ $node == a ]] && ns="$NS_A" || ns="$NS_B"
    helm --kube-context "$CTX" upgrade --install "terrapod-$node" "$REPO_ROOT/helm/terrapod" \
      -n "$ns" \
      -f "$REPO_ROOT/helm/terrapod/values-ha-$node.yaml" \
      --set "api.image.repository=${api_image%:*}" \
      --set "api.image.tag=${api_image##*:}" \
      --set "migrations.image.repository=${mig_image%:*}" \
      --set "migrations.image.tag=${mig_image##*:}" \
      --set "migrations.image.pullPolicy=Never" \
      --wait --timeout 6m
    ok "node $node deployed into $ns"
  done
}

_status() { # $1 = a|b ; prints the ha/status attributes as JSON
  local fn=api_a
  [[ "$1" == b ]] && fn=api_b
  $fn python -c "
import asyncio, json
from terrapod.db.session import get_db_session, init_db
from terrapod.config import settings
from terrapod.services import replication
from terrapod.services import ha_role

async def main():
    await init_db()
    async with get_db_session() as db:
        s = await replication.read_status(db)
    # The inbound credential is config, not a row (#1171).
    inb = settings.ha.peer.inbound
    print(json.dumps({
        'role': await ha_role.get_role(),
        'last_sync_at': str(s.last_sync_at) if s.last_sync_at else None,
        'backfilling': s.backfilling,
        'events_behind': s.events_behind,
        'events_retained': s.events_retained,
        'inbound_configured': bool(inb.client_id and inb.client_secret),
    }))
asyncio.run(main())
" 2>/dev/null | tail -1
}

cmd_verify() {
  info "peer credentials reconciled from config"
  local sa sb
  sa=$(_status a); sb=$(_status b)
  [[ $(print -r -- "$sa" | python3 -c 'import sys,json;print(json.load(sys.stdin)["inbound_configured"])') == True ]] \
    || fail "node A did not materialise its inbound credential from config: $sa"
  [[ $(print -r -- "$sb" | python3 -c 'import sys,json;print(json.load(sys.stdin)["inbound_configured"])') == True ]] \
    || fail "node B did not materialise its inbound credential from config: $sb"
  ok "both nodes reconciled an inbound credential — no CLI step was used"

  info "creating a workspace on the leader"
  local name="ha-smoke-$(date +%s)"
  api_a python -c "
import asyncio
from terrapod.db.session import get_db_session, init_db
from terrapod.db.models import Workspace
from terrapod.services import replication

async def main():
    await init_db()
    # The outbox is a before_flush hook the API installs during startup. This
    # is not that process, so install it here too - otherwise the row lands and
    # no event is ever recorded, and the pair looks broken when it is not.
    # (No backticks: this snippet is inside a double-quoted zsh string, which
    # would command-substitute them.)
    replication.install_outbox_hooks()
    async with get_db_session() as db:
        db.add(Workspace(name='$name'))
        await db.commit()
asyncio.run(main())
"
  ok "created $name on node A"

  info "waiting for it to reach the follower"
  local found=0
  for i in {1..30}; do
    if api_b python -c "
import asyncio, sys
from sqlalchemy import select
from terrapod.db.session import get_db_session, init_db
from terrapod.db.models import Workspace

async def main():
    await init_db()
    async with get_db_session() as db:
        row = await db.scalar(select(Workspace).where(Workspace.name == '$name'))
    sys.exit(0 if row else 1)
asyncio.run(main())
" 2>/dev/null; then found=1; break; fi
    sleep 2
  done
  [[ $found == 1 ]] || fail "the workspace never replicated to node B"
  ok "replicated to node B"

  info "follower reports how far behind it is"
  sb=$(_status b)
  print -r -- "  $sb"
  python3 -c "
import json,sys
s = json.loads('''$sb''')
if s['events_behind'] is None:
    sys.exit('events_behind is unknown — the leader is not reporting its latest id')
print('  events_behind =', s['events_behind'])
" || fail "lag reporting is not working"
  ok "lag is a number, not just a timestamp"

  info "the follower refuses to originate"
  local code
  code=$(api_b python -c "
import asyncio
from httpx import ASGITransport, AsyncClient
from terrapod.api.app import app

async def main():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as c:
        r = await c.post('/api/terrapod/v1/workspaces', json={})
    print(r.status_code)
asyncio.run(main())
" 2>/dev/null | tail -1)
  [[ "$code" == "503" ]] || fail "follower answered $code to a mutating request, expected 503"
  ok "follower returns 503 on writes"

  # The row and the object are two halves of one artifact, and they were built
  # and tested separately. Each was correct on its own terms; nothing asserted
  # the pair. #1175 was exactly that gap - the state file arrived on the
  # follower and no row pointed at it, so a promoted node would have planned the
  # whole estate as a first-time create.
  info "state arrives as BOTH a row and an object"
  local sname="ha-state-$(date +%s)"
  local skey
  skey=$(api_a python -c "
import asyncio, uuid
from terrapod.db.session import get_db_session, init_db
from terrapod.db.models import Workspace, StateVersion
from terrapod.services import replication
from terrapod.storage import get_storage, init_storage, keys

async def main():
    await init_db(); await init_storage()
    replication.install_outbox_hooks()
    async with get_db_session() as db:
        ws = Workspace(name='$sname'); db.add(ws); await db.flush()
        sv = StateVersion(workspace_id=ws.id, serial=1, md5='0'*32, lineage=str(uuid.uuid4()))
        db.add(sv); await db.flush()
        key = keys.state_key(str(ws.id), str(sv.id))
        await db.commit()
    await get_storage().put(key, b'{\"version\":4,\"serial\":1}')
    print(key)
asyncio.run(main())
" 2>/dev/null | tail -1)
  [[ -n "$skey" ]] || fail "could not write a state version on the leader"

  local got=""
  for i in {1..40}; do
    got=$(api_b python -c "
import asyncio
from sqlalchemy import select
from terrapod.db.session import get_db_session, init_db
from terrapod.db.models import Workspace, StateVersion
from terrapod.storage import get_storage, init_storage

async def main():
    await init_db(); await init_storage()
    async with get_db_session() as db:
        ws = await db.scalar(select(Workspace).where(Workspace.name == '$sname'))
        rows = 0
        if ws:
            rows = len((await db.scalars(select(StateVersion).where(StateVersion.workspace_id == ws.id))).all())
    try:
        await get_storage().get('$skey'); blob = 1
    except Exception:
        blob = 0
    print(f'{rows}:{blob}')
asyncio.run(main())
" 2>/dev/null | tail -1)
    [[ "$got" == "1:1" ]] && break
    sleep 3
  done

  case "$got" in
    1:1) ok "the state version arrived as a row AND an object" ;;
    0:1) fail "the object copied but no row names it - a promoted node would plan the estate as a first-time create (#1175)" ;;
    1:0) fail "the row replicated but its object did not - the workspace lists a version that cannot be read" ;;
    *)   fail "state reached the follower as neither a row nor an object (got '$got')" ;;
  esac
}

cmd_reverse() {
  info "swapping roles — A becomes follower, B becomes leader"
  helm --kube-context "$CTX" upgrade terrapod-b "$REPO_ROOT/helm/terrapod" \
    -n "$NS_B" -f "$REPO_ROOT/helm/terrapod/values-ha-b.yaml" \
    --set api.config.ha.role=leader --reuse-values --wait --timeout 5m
  helm --kube-context "$CTX" upgrade terrapod-a "$REPO_ROOT/helm/terrapod" \
    -n "$NS_A" -f "$REPO_ROOT/helm/terrapod/values-ha-a.yaml" \
    --set api.config.ha.role=follower --reuse-values --wait --timeout 5m

  info "creating a workspace on the NEW leader"
  local name="ha-reverse-$(date +%s)"
  api_b python -c "
import asyncio
from terrapod.db.session import get_db_session, init_db
from terrapod.db.models import Workspace
from terrapod.services import replication

async def main():
    await init_db()
    # The outbox is a before_flush hook the API installs during startup. This
    # is not that process, so install it here too - otherwise the row lands and
    # no event is ever recorded, and the pair looks broken when it is not.
    # (No backticks: this snippet is inside a double-quoted zsh string, which
    # would command-substitute them.)
    replication.install_outbox_hooks()
    async with get_db_session() as db:
        db.add(Workspace(name='$name'))
        await db.commit()
asyncio.run(main())
"
  local found=0
  for i in {1..30}; do
    if api_a python -c "
import asyncio, sys
from sqlalchemy import select
from terrapod.db.session import get_db_session, init_db
from terrapod.db.models import Workspace

async def main():
    await init_db()
    async with get_db_session() as db:
        row = await db.scalar(select(Workspace).where(Workspace.name == '$name'))
    sys.exit(0 if row else 1)
asyncio.run(main())
" 2>/dev/null; then found=1; break; fi
    sleep 2
  done
  [[ $found == 1 ]] || fail "reversed direction never replicated — the credentials for it were not in place"
  ok "replication works in the reversed direction, with no credential exchange"
}

cmd_down() {
  helm --kube-context "$CTX" uninstall terrapod-a -n "$NS_A" 2>/dev/null || true
  helm --kube-context "$CTX" uninstall terrapod-b -n "$NS_B" 2>/dev/null || true
  k delete namespace "$NS_A" "$NS_B" --ignore-not-found >/dev/null
  ok "pair removed"
}

case "${1:-}" in
  up) cmd_up ;;
  verify) cmd_verify ;;
  reverse) cmd_reverse ;;
  table) shift; python3 "$REPO_ROOT/scripts/ha_smoke_table.py" "$@" ;;
  down) cmd_down ;;
  *) print "usage: $0 {up|verify|reverse|table [row...]|down}"; exit 2 ;;
esac
