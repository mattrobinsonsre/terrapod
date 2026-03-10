#!/usr/bin/env bash
# Lint all components in Docker.
# Usage: scripts/lint.sh [python|all]
# Default: all

set -euo pipefail
source "$(dirname "$0")/lib.sh"

lint_python() {
  info "Linting Python..."
  ensure_test_image
  docker compose -f "$REPO_ROOT/docker-compose.test.yml" run --rm lint
  success "Python lint passed"
}

lint_web() {
  info "Linting frontend..."
  docker run --rm -v "$REPO_ROOT/web:/app" -w /app node:24-alpine sh -c "npm ci && npm run lint && npm run type-check"
  success "Frontend lint passed"
}

target="${1:-all}"

case "$target" in
  python) lint_python ;;
  web) lint_web ;;
  all)
    lint_python
    lint_web
    success "All linters passed"
    ;;
  *)
    error "Unknown target: $target"
    echo "Usage: $0 [python|web|all]"
    exit 1
    ;;
esac
