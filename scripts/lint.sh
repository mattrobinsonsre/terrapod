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

target="${1:-all}"

case "$target" in
  python) lint_python ;;
  all)
    lint_python
    success "All linters passed"
    ;;
  *)
    error "Unknown target: $target"
    echo "Usage: $0 [python|all]"
    exit 1
    ;;
esac
