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
error()   { printf '\033[1;31m==> ERROR: %s\033[0m\n' "$*" >&2; }

# ── Docker helpers ────────────────────────────────────────

# Build the Python test image if needed.
TEST_IMAGE="${TEST_IMAGE:-terrapod-test:local}"
ensure_test_image() {
  info "Building Python test image ($TEST_IMAGE)..."
  docker build -f "$REPO_ROOT/docker/Dockerfile.test" -t "$TEST_IMAGE" "$REPO_ROOT"
}
