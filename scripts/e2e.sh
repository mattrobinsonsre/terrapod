#!/usr/bin/env bash
# Run Playwright E2E tests against a Docker Compose stack.
# Usage: scripts/e2e.sh

set -euo pipefail
source "$(dirname "$0")/lib.sh"

COMPOSE_FILE="$REPO_ROOT/e2e/docker-compose.e2e.yml"
E2E_DIR="$REPO_ROOT/e2e"

cleanup() {
  info "Tearing down E2E stack..."
  docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
}
trap cleanup EXIT

# Always rebuild. This used to build only when the image was ABSENT, which
# meant the suite ran against whatever `terrapod-api:local` happened to be —
# potentially weeks old — while the specs came from the working tree. That
# produces false failures (a spec written for a feature the image predates)
# and, worse, false passes: a spec written to guard a regression passes
# happily against an image built before the regression existed. Layer caching
# makes a no-change rebuild cheap, so the guard bought very little. (#1281)
info "Building images from the working tree..."
docker build -f "$REPO_ROOT/docker/Dockerfile.api" -t terrapod-api:local "$REPO_ROOT"
docker build -f "$REPO_ROOT/docker/Dockerfile.web" -t terrapod-web:local "$REPO_ROOT"
docker build -f "$REPO_ROOT/docker/Dockerfile.migrations" -t terrapod-migrations:local "$REPO_ROOT"

# Start the stack
info "Starting E2E stack..."
docker compose -f "$COMPOSE_FILE" up -d

# Wait for the web service to be healthy
info "Waiting for stack health..."
timeout=120
elapsed=0
while ! curl -sf http://localhost:3000/ > /dev/null 2>&1; do
  if (( elapsed >= timeout )); then
    error "Stack did not become healthy within ${timeout}s"
    docker compose -f "$COMPOSE_FILE" logs
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done
info "Stack is healthy (${elapsed}s)"

# Install Playwright dependencies
info "Installing Playwright dependencies..."
cd "$E2E_DIR"
npm ci --ignore-scripts
npx playwright install chromium --with-deps 2>/dev/null || npx playwright install chromium

# Run tests
info "Running Playwright E2E tests..."
npx playwright test

success "E2E tests passed"
