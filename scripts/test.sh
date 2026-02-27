#!/usr/bin/env bash
# Run all tests in Docker.
# Usage: scripts/test.sh [python|all]
# Default: all

set -euo pipefail
source "$(dirname "$0")/lib.sh"

test_python() {
  info "Testing Python..."
  ensure_test_image
  docker compose -f "$REPO_ROOT/docker-compose.test.yml" run --rm test
  success "Python tests passed"
}

target="${1:-all}"

case "$target" in
  python) test_python ;;
  all)
    test_python
    success "All tests passed"
    ;;
  *)
    error "Unknown target: $target"
    echo "Usage: $0 [python|all]"
    exit 1
    ;;
esac
