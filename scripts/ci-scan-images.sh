#!/usr/bin/env bash
#
# Scan every Terrapod image for ONE architecture with Trivy, and fail if any
# image was not scanned.
#
# Called by `scan-images-amd64` and `scan-images-arm64` in ci.yml. It lives in a
# file rather than inline because those two jobs are deliberately separate --
# each waits on only its OWN architecture's build, so a slow or failing arm64
# build neither delays nor suppresses the amd64 scan -- and two copies of this
# logic in the workflow would be two things to keep in step.
#
# WHY ONE JOB PER ARCHITECTURE, AND SEQUENTIAL SCANS INSIDE IT
#
# Trivy takes an EXCLUSIVE LOCK on its cache directory. Measured: five scans
# running in parallel over one --cache-dir left one holding the lock and the
# rest dead within five seconds --
#     ERROR Failed to acquire cache or database lock
#     FATAL unable to initialize a scan service: unable to initialize cache
# -- so six of ten images went unscanned while the step "finished" in 35
# seconds. Two jobs are two runners and therefore two caches, so the contention
# cannot arise rather than having to be managed with a per-worker copy of a
# hundreds-of-megabyte vulnerability database.
#
# Architecture is also the cheaper axis to split on: the dominant cost here is
# fetching the images, not scanning them, so per architecture each job handles
# five rather than ten.
#
# Environment (all set by the calling job):
#   ARCH        amd64 | arm64            REGISTRY   the image registry prefix
#   SHA_SHORT   the commit's image tag   IS_PR      "true" on a pull request
#   TRIVY_IMAGE optional override        RUNNER_TEMP optional, defaults to /tmp

set -uo pipefail

: "${ARCH:?ARCH is required}"
: "${REGISTRY:?REGISTRY is required}"
: "${SHA_SHORT:?SHA_SHORT is required}"
IS_PR="${IS_PR:-false}"
TRIVY="${TRIVY_IMAGE:-ghcr.io/aquasecurity/trivy:0.72.0}"
CACHE="${RUNNER_TEMP:-/tmp}/trivy-cache"
# Where the calling job put the downloaded tarballs. Overridable only so
# this script can be driven end to end outside CI -- the guards below are
# the whole point of it, and a guard nobody can exercise is a guard nobody
# knows fires.
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/tmp}"

IMAGES=(terrapod-api terrapod-web terrapod-runner terrapod-listener terrapod-migrations)

# ── the images ───────────────────────────────────────────────────────
if [ "$IS_PR" = "true" ]; then
  # Checked BEFORE the loop, because the pattern download in the calling job is
  # the one step that can silently fetch nothing -- a rename on the build side
  # leaves it matching zero artifacts. Counting afterwards would be a gate that
  # cannot fire, since the first `gunzip` would already have failed; counting
  # first is what names the actual cause.
  present=$(find "$DOWNLOAD_DIR" -maxdepth 1 -name "terrapod-*-${ARCH}.tar.gz" | wc -l | tr -d " ")
  if [ "$present" -ne "${#IMAGES[@]}" ]; then
    echo "expected ${#IMAGES[@]} image tarballs for ${ARCH}, found ${present}."
    echo "The download pattern matched nothing, or the artifact names moved."
    exit 1
  fi
  for img in "${IMAGES[@]}"; do
    gunzip -c "${DOWNLOAD_DIR}/${img}-${ARCH}.tar.gz" | docker load || exit 1
  done
else
  for img in "${IMAGES[@]}"; do
    docker pull "${REGISTRY}/${img}:sha-${SHA_SHORT}-${ARCH}" || exit 1
  done
fi

# ── the vulnerability database, once ─────────────────────────────────
# With the same bounded retry `(go-audit)` uses in ci.yml: a registry hiccup
# here must be its own attributable failure rather than five scans each
# reporting zero findings because no database arrived.
mkdir -p "$CACHE"
db_ok=0
for attempt in 1 2 3; do
  if docker run --rm -v "${CACHE}:/cache" "$TRIVY" \
       image --cache-dir /cache --download-db-only --quiet; then
    db_ok=1
    break
  fi
  [ "$attempt" -lt 3 ] && sleep $((attempt * 15))
done
if [ "$db_ok" != "1" ]; then
  echo "Could not download the Trivy vulnerability database after 3 attempts."
  echo "NOTHING WAS SCANNED -- an infrastructure failure, not a clean result."
  exit 1
fi

# ── the scans ────────────────────────────────────────────────────────
for img in "${IMAGES[@]}"; do
  docker run --rm \
    -v "${CACHE}:/cache" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "${PWD}:/src" -w /src \
    -e TRIVY_PLATFORM="linux/${ARCH}" \
    "$TRIVY" image \
      --cache-dir /cache --skip-db-update --skip-version-check \
      --severity HIGH,CRITICAL --ignore-unfixed \
      --ignorefile /src/pentest/trivy/.trivyignore \
      --format sarif --output "/src/trivy-${img}-${ARCH}.sarif" \
      --exit-code 0 \
      "${REGISTRY}/${img}:sha-${SHA_SHORT}-${ARCH}" > "scan-${img}.log" 2>&1
  echo "$?" > "rc-${img}"
done

# ── the verdict ──────────────────────────────────────────────────────
# Every scan must have produced a SARIF. A missing or empty one is an absent
# signal, and an absent signal must not read as a clean image.
fail=0
scanned=0
for img in "${IMAGES[@]}"; do
  rc=$(cat "rc-${img}" 2>/dev/null || echo 99)
  if [ ! -s "trivy-${img}-${ARCH}.sarif" ] || [ "$rc" != "0" ]; then
    echo "::error::trivy did not complete for ${img} (${ARCH}), rc=${rc}"
    sed -n '1,40p' "scan-${img}.log" 2>/dev/null || true
    fail=1
    continue
  fi
  n=$(jq '[.runs[].results[]?] | length' "trivy-${img}-${ARCH}.sarif")
  printf '%-24s %-6s %s finding(s)\n' "$img" "$ARCH" "$n"
  scanned=$((scanned + 1))
  [ "$n" -gt 0 ] && fail=1
done

# A floor, because every check above is per-image: if IMAGES itself stopped
# being populated, the loops would iterate over nothing and this would report
# success having scanned none at all.
echo "images scanned cleanly: ${scanned} of ${#IMAGES[@]}"
if [ "$scanned" -eq 0 ]; then
  echo "::error::no image was scanned, so this job proved nothing."
  echo "That is an infrastructure failure, not a pass."
  fail=1
fi

exit "$fail"
