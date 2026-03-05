# Terrapod Makefile
# Thin wrapper around scripts/*.sh — all build logic lives in scripts.
#
# Docker-first: lint, test, build, and publish all run in containers.

.PHONY: lint lint-python \
	test test-python \
	build images \
	publish publish-images publish-chart publish-release \
	release dev dev-down \
	clean test-down \
	help

# ── Lint ──────────────────────────────────────────────────
lint:               ## Lint all (Python) in Docker
	scripts/lint.sh

lint-python:        ## Lint Python only (Docker)
	scripts/lint.sh python

# ── Test ──────────────────────────────────────────────────
test:               ## Test all (Python) in Docker
	scripts/test.sh

test-python:        ## Test Python only (Docker)
	scripts/test.sh python

# ── Build ─────────────────────────────────────────────────
images:             ## Build Docker images (single-arch, local)
	docker build -f docker/Dockerfile.api -t terrapod-api:local .
	docker build -f docker/Dockerfile.web -t terrapod-web:local .

# ── Publish ───────────────────────────────────────────────
publish:            ## Build + push multi-arch images to GHCR
	scripts/publish.sh images

publish-images:     ## Push multi-arch images to GHCR
	scripts/publish.sh images

publish-chart:      ## Push Helm chart to OCI registry
	scripts/publish.sh chart

publish-release:    ## Create GitHub Release with auto-generated notes
	scripts/publish.sh release

# ── Release ───────────────────────────────────────────────
release:            ## Full release: lint, test, publish all
	scripts/lint.sh
	scripts/test.sh
	scripts/publish.sh

# ── Development ──────────────────────────────────────────
dev:                ## Start Tilt development environment (port 10352)
	tilt up --port 10352

dev-down:           ## Stop Tilt
	tilt down --port 10352

# ── Utility ──────────────────────────────────────────────
clean:              ## Clean build artifacts
	rm -rf services/.pytest_cache services/.coverage services/htmlcov
	rm -rf dist/

test-down:          ## Tear down test containers
	docker compose -f docker-compose.test.yml down -v

help:               ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
