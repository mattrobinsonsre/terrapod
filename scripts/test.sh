#!/usr/bin/env bash
# Run all tests in Docker.
# Usage: scripts/test.sh [python|all] [-- pytest args...]
# Examples:
#   scripts/test.sh                              # all targets, all tests
#   scripts/test.sh python                       # all python tests
#   scripts/test.sh python -- tests/services/    # only the services suite
#   scripts/test.sh python -- -x tests/api/      # services suite, fail-fast
# Default target: all

set -euo pipefail
source "$(dirname "$0")/lib.sh"

# Extra args after `--` are passed through to pytest inside the
# container. CI shards Python Test by directory via this hook.
PYTEST_ARGS=()
target="${1:-all}"
shift || true
if [[ "${1:-}" == "--" ]]; then
  shift
  PYTEST_ARGS=("$@")
fi

test_python() {
  info "Testing Python..."
  ensure_test_image
  if [[ ${#PYTEST_ARGS[@]} -eq 0 ]]; then
    docker compose -f "$REPO_ROOT/docker-compose.test.yml" run --rm test
  else
    docker compose -f "$REPO_ROOT/docker-compose.test.yml" \
      run --rm test pytest "${PYTEST_ARGS[@]}"
  fi
  success "Python tests passed"
}

case "$target" in
  python) test_python ;;
  all)
    test_python
    success "All tests passed"
    ;;
  *)
    error "Unknown target: $target"
    echo "Usage: $0 [python|all] [-- pytest args...]"
    exit 1
    ;;
esac
