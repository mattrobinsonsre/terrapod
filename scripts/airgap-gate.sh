#!/usr/bin/env bash
# Prove the air-gap claim with real clients and no route to the internet (#1485).
#
# #1407 §12 states the phase-0 acceptance in one sentence: *run each client with
# upstream network blocked*. Every surface has met that by hand, once, when it
# was built. This makes it a standing guarantee.
#
# Two phases against one running stack:
#
#   warm     the client has internet; each install pulls through Terrapod once,
#            which is what populates the cache.
#   blocked  the client is moved onto a network with NO gateway and every install
#            is repeated. Anything that reaches upstream fails here.
#
# The blocked client can still reach Terrapod — `--network none` would prove
# nothing, since the claim is "no internet", not "no network". Both phases use
# the SAME URL (http://web:3000), so nothing about the address varies between
# them; only the route to the outside world does.
#
# **The self-check is what makes this a test rather than a tautology.** Before
# any client runs blocked, the blocked network is proven to have no route out. A
# harness that silently kept internet access would pass every row while asserting
# nothing, and would look identical to a real pass.
#
# Client-side caches are defeated per client (`pip --no-cache-dir`, a throwaway
# npm cache, `GOMODCACHE` in the container). Without that an install that never
# touched the proxy reads as a passing test — a trap this repository has already
# hit once.
#
# The api container is deliberately NOT recreated between phases. The e2e compose
# stack mounts no volume on the storage dir, so replacing that container discards
# every stored object while Postgres keeps the rows naming them — which presents
# as "cached artifact 404s" and reads like a cache bug. Only the client moves.
#
#     scripts/airgap-gate.sh [base-url]
#
# Defaults to the e2e stack's BFF at http://localhost:3000. Point it at the BFF,
# never the API directly: that hop is the only address a deployment exposes, and
# it is where several of these surfaces' bugs have actually lived.

set -euo pipefail

# `--gated-off` asserts the other half of the engine gate (#1429): with the
# ansible and pulumi engines switched off, these surfaces are *absent* rather
# than answering 404, while terraform's own surfaces still answer. It needs no
# clients, only a stack booted with the engines disabled.
MODE="run"
if [ "${1:-}" = "--gated-off" ]; then MODE="gated-off"; shift; fi

BASE="${1:-http://localhost:3000}"
NET="tp-airgap-$$"
PREFIX="/api/terrapod/v1/package-cache"
#: Reached by service alias from inside the blocked network.
INNER="http://web:3000"

PASS=0
FAIL=0
RESULTS=()
LOGS="$(mktemp -d)"

cleanup() {
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

note() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { PASS=$((PASS + 1)); RESULTS+=("PASS  $*"); printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
bad()  { FAIL=$((FAIL + 1)); RESULTS+=("FAIL  $*"); printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
# A skip is recorded and printed, never silently dropped: a row that cannot run
# here must be visible in the summary, or the gate quietly shrinks over time.
skip() { RESULTS+=("SKIP  $*"); printf '  \033[33mSKIP\033[0m  %s\n' "$*"; }


# ── gated-off mode ─────────────────────────────────────────────────────

if [ "$MODE" = "gated-off" ]; then
  note "Engine gate: ansible and pulumi disabled"
  curl -sf "$BASE/" >/dev/null || { echo "no stack at $BASE"; exit 1; }

  schema="$(curl -sf "$BASE/api/openapi.json")" || { echo "no schema"; exit 1; }
  counts="$(printf '%s' "$schema" | python3 -c "
import json, sys
paths = list(json.load(sys.stdin).get('paths', {}))
print(sum('/package-cache/' in p for p in paths),
      sum(p.startswith('/v2/') for p in paths),
      sum('/providers/' in p for p in paths),
      sum('registry-modules' in p for p in paths),
      sum('binary-cache' in p for p in paths))
")"
  read -r pkg oci prov mods bins <<<"$counts"

  # Absent, not 404ing. A route that still exists and answers 404 would look
  # identical from the outside; the schema is what distinguishes them.
  [ "$pkg" -eq 0 ] && ok "package-cache routes absent from the schema ($pkg)" \
                   || bad "package-cache routes still registered ($pkg)"
  [ "$oci" -eq 0 ] && ok "OCI /v2/ routes absent from the schema ($oci)" \
                   || bad "OCI /v2/ routes still registered ($oci)"

  # ...while terraform's own surfaces are untouched. Without this half, deleting
  # every route would pass.
  [ "$prov" -gt 0 ] && ok "provider mirror still registered ($prov)" \
                    || bad "provider mirror disappeared ($prov)"
  [ "$mods" -gt 0 ] && ok "module registry still registered ($mods)" \
                    || bad "module registry disappeared ($mods)"
  [ "$bins" -gt 0 ] && ok "engine binary cache still registered ($bins)" \
                    || bad "engine binary cache disappeared ($bins)"

  # And the deployed path agrees with the schema.
  code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE$PREFIX/pypi/simple/six/")"
  [ "$code" = "404" ] && ok "a gated-off path 404s through the BFF ($code)" \
                      || bad "a gated-off path answered $code, expected 404"

  note "Result"
  printf '  %s\n' "${RESULTS[@]}"
  printf '\n  %d passed, %d failed\n' "$PASS" "$FAIL"
  [ "$FAIL" -eq 0 ] || exit 1
  exit 0
fi

# ── stack + credentials ────────────────────────────────────────────────

note "Stack"
curl -sf "$BASE/" >/dev/null || { echo "no stack at $BASE"; exit 1; }
echo "  reachable at $BASE"

TOKEN="$(python3 scripts/mint-token.py --url "$BASE" \
         --email admin@terrapod.local --password 'TestPassword123!')"
[ -n "$TOKEN" ] || { echo "could not mint a token"; exit 1; }
echo "  minted an API token"

# By compose service label rather than `--filter publish=`, which podman rejects
# outright ("publish is an invalid filter") — and this harness has to run on both
# engines, since CI is docker and local development is podman.
WEB_CID="$(docker ps --filter 'label=com.docker.compose.service=web' --format '{{.ID}}' | head -1)"
[ -n "$WEB_CID" ] || { echo "could not find the web container"; exit 1; }

# The stack's own network. The warm phase runs there because `web` resolves AND
# it has a route out, so warm and blocked can use the identical URL and differ in
# exactly one thing: whether the internet is reachable.
STACK_NET="$(docker inspect "$WEB_CID" \
  --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')"
[ -n "$STACK_NET" ] || { echo "could not determine the stack network"; exit 1; }
echo "  stack network: $STACK_NET"

# ── the blocked network ────────────────────────────────────────────────
#
# `--internal` gives the network no gateway, so a container attached to it alone
# has no route off the host. The BFF is attached too, under the alias `web`, so
# Terrapod stays reachable — which is the whole point.

note "Blocked network"
docker network create --internal "$NET" >/dev/null
docker network connect --alias web "$NET" "$WEB_CID"
echo "  created $NET (internal) and attached the BFF as 'web'"

# ── the self-check ─────────────────────────────────────────────────────

note "Self-check: is the blocked network actually blocked?"
if docker run --rm --network "$NET" curlimages/curl:8.11.1 \
     -sS --max-time 8 https://pypi.org/simple/ >/dev/null 2>&1; then
  bad "blocked network reached pypi.org — every result below would be meaningless"
  echo
  echo "The harness cannot assert anything while the blocked network has a route out."
  exit 1
else
  ok "blocked network cannot reach pypi.org"
fi

if docker run --rm --network "$NET" curlimages/curl:8.11.1 \
     -sS --max-time 8 "$INNER/" >/dev/null 2>&1; then
  ok "blocked network can still reach Terrapod"
else
  bad "blocked network cannot reach Terrapod — nothing below can pass"
  exit 1
fi

# ── surfaces ───────────────────────────────────────────────────────────
#
# Each surface runs warm then blocked. The blocked run is the assertion; the warm
# run exists to populate the cache and to prove the row works at all.

surface() {
  local name="$1" image="$2" script="$3"
  note "$name"

  # Pull first and separately, so image-transfer chatter cannot bury the error
  # the row actually produced.
  docker pull -q "$image" >/dev/null 2>&1 || true

  local warm="$LOGS/${name// /-}.warm.log" blocked="$LOGS/${name// /-}.blocked.log"

  if docker run --rm --network "$STACK_NET" "$image" sh -ec "$script" >"$warm" 2>&1; then
    ok "$name warm (through Terrapod, with internet)"
  else
    bad "$name warm — the row itself is broken, not the air gap"
    tail -12 "$warm" | sed 's/^/        /'
    return
  fi

  if docker run --rm --network "$NET" "$image" sh -ec "$script" >"$blocked" 2>&1; then
    ok "$name blocked (no route to the internet)"
  else
    bad "$name blocked — it needs upstream"
    tail -12 "$blocked" | sed 's/^/        /'
  fi
}

surface "PyPI" python:3.12-slim "
  pip install --no-cache-dir --quiet --disable-pip-version-check \
    --index-url 'http://tp:$TOKEN@web:3000$PREFIX/pypi/simple/' \
    --trusted-host web six
  python -c 'import six; print(six.__version__)'
"

surface "npm" node:22-alpine "
  mkdir -p /tmp/proj && cd /tmp/proj
  # A dedicated cache dir, or an install that never touched the proxy passes.
  printf '%s\n' \
    'registry=http://web:3000$PREFIX/npm/' \
    '//web:3000$PREFIX/npm/:_authToken=$TOKEN' \
    'cache=/tmp/npmcache' > .npmrc
  npm install --silent --no-audit --no-fund left-pad
  node -e \"require('/tmp/proj/node_modules/left-pad')\"
"

# Two Go settings will silently defeat this row, and one of them passed the warm
# phase while proving nothing:
#   GOPRIVATE / GONOPROXY  — make the toolchain bypass the proxy and resolve the
#                            module directly from its VCS host. With internet that
#                            looks like a pass; blocked, it is the failure.
#   GOPROXY=...,direct     — falls through to upstream on a proxy miss, which is
#                            exactly the hole this gate exists to find.
# So GOPROXY names the proxy and nothing else, and GOPRIVATE stays unset.
if [ "${BASE#https://}" = "$BASE" ]; then
  skip "Go modules — needs HTTPS: the toolchain refuses to send credentials to a
        plain-HTTP URL ('refusing to pass credentials to insecure URL'), and
        neither GOINSECURE nor .netrc lifts it. Confirmed by experiment here and
        already recorded in docs/package-cache.md. Run this gate against an
        HTTPS base URL to cover the row."
else
surface "Go modules" golang:1.23 "
  export GOMODCACHE=/tmp/gomod GOFLAGS=-mod=mod GOSUMDB=off
  export GOPROXY='http://tp:$TOKEN@web:3000$PREFIX/go'
  mkdir -p /tmp/m && cd /tmp/m && go mod init probe >/dev/null
  go mod edit -require=rsc.io/quote@v1.5.2
  go mod download rsc.io/quote
  test -d \"\$GOMODCACHE/rsc.io\"
"
fi

# ── summary ────────────────────────────────────────────────────────────

note "Result"
printf '  %s\n' "${RESULTS[@]}"
printf '\n  %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
