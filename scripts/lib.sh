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

# Where to look for a published test BASE image. Empty means "nowhere", which is
# the default so a clone with no registry access behaves exactly as it always
# has: it builds the base itself, once.
TEST_BASE_REGISTRY="${TEST_BASE_REGISTRY:-}"

# The base image's tag: `sha-<hash of everything that goes into it>` (#2248).
#
# The `sha-` prefix is not decoration. GHCR cleanup keeps every version
# carrying a RELEASE tag for ever and reaps the ones whose tags are only
# sha-shaped, so prefixing this makes a cache artifact reapable BY SHAPE
# rather than by an exemption somebody has to remember -- and reaping it is
# harmless, because step 3 below rebuilds it.
#
# **Content-addressed on purpose.** The tag has to describe the image, because a
# moving tag would let a run test against dependencies that are not the ones its
# lock names -- and that is the whole failure `poetry.lock` exists to prevent
# (GHSA-rgvw-c74c-h75w). Change the lock and the tag changes with it, so a stale
# base is a cache MISS rather than a wrong answer.
#
# These three files are the complete input set, which is checkable:
# `Dockerfile.test-base` pins its own tool versions as ARG defaults, and the only
# things it COPYs are the two manifests. If you add a COPY there, add it here --
# `check_test_base_inputs.py` fails the build if the two drift apart.
#
# Not covered, deliberately: `python:3.14-slim` is a moving tag, so a new CPython
# patch does not change the hash. That is the same exposure every image here has
# and is what `PKG_REFRESH` handles for the published ones; for a test image the
# cost of being a few days behind is nil.
_test_base_hash() {
  local sum
  if command -v sha256sum >/dev/null 2>&1; then
    sum=sha256sum
  else
    # macOS has no sha256sum; shasum is in the base install on both.
    sum="shasum -a 256"
  fi
  cat "$REPO_ROOT/docker/Dockerfile.test-base" \
      "$REPO_ROOT/services/pyproject.toml" \
      "$REPO_ROOT/services/poetry.lock" | $sum | cut -c1-16
}

# Resolve the base image, then build the leaf on it.
#
# Every Python CI slice used to build the WHOLE image from scratch, because a
# fresh runner has no layer store: 64s per job measured on run 40316874372, of
# which ~45s was the base's work and ~17s was exporting layers. Fifteen slices
# paid it fifteen times for an image whose expensive half changes only when the
# lock does.
#
# Three-step resolution, and the order matters:
#
#   1. already in the local store -> use it. This is what makes a developer's
#      second `make test` fast, which it never was before.
#   2. pullable from `TEST_BASE_REGISTRY` -> pull it. A pull writes straight into
#      the daemon, so unlike a buildx `--load` it pays no export/import tax --
#      measured at ~25-47s, which is why caching the build lost to pulling it.
#   3. otherwise build it. This is the fallback that keeps a fork PR, an
#      air-gapped checkout and a dependency bump all working with no special
#      case, and it is why reaping a published base is harmless: the next build
#      makes it again.
#
# The leaf is then a plain `docker build` of COPY layers measured at 0.0-0.1s
# each, so it stays a `docker build` -- buildx would add an export round trip to
# save nothing.
ensure_test_image() {
  check_container_store_space || return 1

  local hash base
  hash="$(_test_base_hash)"
  base="terrapod-test-base:sha-${hash}"

  if ! docker image inspect "$base" >/dev/null 2>&1; then
    if [[ -n "$TEST_BASE_REGISTRY" ]]; then
      local remote="${TEST_BASE_REGISTRY}/terrapod-test-base:sha-${hash}"
      if docker image inspect "$remote" >/dev/null 2>&1 || docker pull "$remote"; then
        base="$remote"
      fi
    fi
  fi

  if ! docker image inspect "$base" >/dev/null 2>&1; then
    info "Building the test base image ($base)..."
    docker build -f "$REPO_ROOT/docker/Dockerfile.test-base" -t "$base" "$REPO_ROOT"
  else
    info "Using test base image $base"
  fi

  info "Building Python test image ($TEST_IMAGE)..."
  docker build -f "$REPO_ROOT/docker/Dockerfile.test" \
    --build-arg "TEST_BASE_IMAGE=${base}" -t "$TEST_IMAGE" "$REPO_ROOT"
}

# The resolved base tag, for a caller that needs to publish it.
test_base_tag() {
  printf 'terrapod-test-base:sha-%s\n' "$(_test_base_hash)"
}
