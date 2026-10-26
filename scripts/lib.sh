#!/usr/bin/env bash
# Shared variables and helpers for Terrapod build scripts.
# Sourced by all other scripts in scripts/.

set -euo pipefail

# ── Repo root ─────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── Version info ──────────────────────────────────────────
VERSION="${VERSION:-$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo "dev")}"
GIT_COMMIT="${GIT_COMMIT:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo "unknown")}"
BUILD_TIME="${BUILD_TIME:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"

# ── Registry and image names ─────────────────────────────
REGISTRY="${REGISTRY:-ghcr.io/mattrobinsonsre}"

# ── Output helpers ────────────────────────────────────────
info()    { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
success() { printf '\033[1;32m==> %s\033[0m\n' "$*"; }
warn()    { printf '\033[1;33m==> WARNING: %s\033[0m\n' "$*"; }
error()   { printf '\033[1;31m==> ERROR: %s\033[0m\n' "$*" >&2; }

# ── Container-store health ────────────────────────────────

# Minimum free space, in GB, required in the container store before a build.
CONTAINER_STORE_MIN_FREE_GB="${CONTAINER_STORE_MIN_FREE_GB:-10}"

# Refuse to build when the container store is nearly full.
#
# Running the store out of space does more than fail the build: podman writes a
# layer's content to disk and then commits its metadata, so a failure between
# the two leaves the layer on disk with nothing referencing it. Those orphans
# are invisible to `podman system prune` and to `podman system check` — neither
# can reclaim what has no metadata entry — so the space is unrecoverable short
# of a storage reset. It also compounds: the fuller the disk, the more metadata
# writes fail, and the faster orphans accumulate.
#
# One workstation reached 9,328 orphaned layers holding ~85 GB this way. Failing
# loudly beforehand is cheap; the alternative is silent, permanent loss.
#
# Advisory on non-podman engines and in CI, where the runner is disposable.
check_container_store_space() {
  command -v podman >/dev/null 2>&1 || return 0
  [ -n "${CI:-}" ] && return 0

  local avail_kb avail_gb
  avail_kb="$(podman machine ssh 'df -Pk /var/lib/containers 2>/dev/null | awk "NR==2 {print \$4}"' 2>/dev/null || true)"
  case "$avail_kb" in ''|*[!0-9]*) return 0 ;; esac   # not a podman machine, or unreadable

  avail_gb=$(( avail_kb / 1024 / 1024 ))
  if [ "$avail_gb" -lt "$CONTAINER_STORE_MIN_FREE_GB" ]; then
    error "Container store has only ${avail_gb}GB free (minimum ${CONTAINER_STORE_MIN_FREE_GB}GB)."
    error "Building now risks orphaning layers that no podman command can reclaim."
    error "Run 'make doctor' to see the damage, and reclaim space before retrying."
    return 1
  fi
  return 0
}

# ── Docker helpers ────────────────────────────────────────

# Build the Python test image if needed.
TEST_IMAGE="${TEST_IMAGE:-terrapod-test:local}"

# `TEST_IMAGE_PREBUILT` says the caller has already put $TEST_IMAGE in the image
# store, so do not build it again. CI sets it: every Python slice used to run
# this plain `docker build` from scratch, because a fresh runner has no layer
# store, which measured 64s per job -- a quarter of an hour of runner time per
# run producing the same image fifteen times.
#
# Explicit rather than "skip if the image exists". A local `make test` runs the
# tests BAKED INTO the image, so skipping on mere presence would silently
# validate a stale one, and an unchanged test count after adding tests is the
# only tell. An env var cannot be set by accident.
#
# A missing image FAILS here rather than falling back to a build: the fallback
# would turn a broken CI step into fifteen slow-but-green jobs, which is the
# shape that hides itself.
ensure_test_image() {
  if [[ -n "${TEST_IMAGE_PREBUILT:-}" ]]; then
    if ! docker image inspect "$TEST_IMAGE" >/dev/null 2>&1; then
      error "TEST_IMAGE_PREBUILT is set but $TEST_IMAGE is not in the image store."
      error "Whatever was meant to build it did not, so there is nothing to test with."
      return 1
    fi
    info "Using the prebuilt Python test image ($TEST_IMAGE)..."
    return 0
  fi
  check_container_store_space || return 1
  info "Building Python test image ($TEST_IMAGE)..."
  docker build -f "$REPO_ROOT/docker/Dockerfile.test" -t "$TEST_IMAGE" "$REPO_ROOT"
}
