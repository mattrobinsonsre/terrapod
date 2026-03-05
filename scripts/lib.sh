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

# ── Validation helpers ───────────────────────────────────

# Require VERSION to be a clean semver tag (vX.Y.Z).
# Call this in any script that publishes artifacts.
require_semver_version() {
  if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]]; then
    error "VERSION '$VERSION' is not valid semver (expected vX.Y.Z or vX.Y.Z-pre.N)"
    error ""
    error "Either:"
    error "  1. Pass VERSION explicitly:  make release VERSION=v0.1.0"
    error "  2. Tag the current commit:   git tag v0.1.0"
    error ""
    error "VERSION is derived from 'git describe --tags'. If HEAD is not"
    error "exactly on a tag, the version includes a commit suffix (e.g."
    error "v0.0.0-2-gd8e9122) which is not valid for Helm charts."
    exit 1
  fi
}

# Check that required CLI tools are available and authenticated.
require_publish_prereqs() {
  local target="${1:-all}"
  local missing=0

  if [[ "$target" == "images" || "$target" == "all" ]]; then
    if ! command -v docker &>/dev/null; then
      error "docker is not installed"
      missing=1
    fi
    if ! docker buildx inspect multiarch-builder &>/dev/null 2>&1; then
      info "Creating docker buildx builder 'multiarch-builder'..."
      docker buildx create --name multiarch-builder --use || {
        error "Failed to create buildx builder"
        missing=1
      }
    fi
  fi

  if [[ "$target" == "chart" || "$target" == "all" ]]; then
    if ! command -v helm &>/dev/null; then
      error "helm is not installed"
      missing=1
    fi
  fi

  if [[ "$target" == "release" || "$target" == "all" ]]; then
    if ! command -v gh &>/dev/null; then
      error "gh CLI is not installed"
      missing=1
    elif ! gh auth status &>/dev/null 2>&1; then
      error "gh CLI is not authenticated — run: gh auth login"
      missing=1
    fi
  fi

  if [[ "$missing" -ne 0 ]]; then
    exit 1
  fi
}

# Ensure working tree is clean (no uncommitted changes).
require_clean_worktree() {
  if [[ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]]; then
    error "Working tree has uncommitted changes. Commit or stash before releasing."
    git -C "$REPO_ROOT" status --short >&2
    exit 1
  fi
}

# ── Docker helpers ────────────────────────────────────────

# Build the Python test image if needed.
TEST_IMAGE="${TEST_IMAGE:-terrapod-test:local}"
ensure_test_image() {
  info "Building Python test image ($TEST_IMAGE)..."
  docker build -f "$REPO_ROOT/docker/Dockerfile.test" -t "$TEST_IMAGE" "$REPO_ROOT"
}
