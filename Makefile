# Terrapod Makefile
# Thin wrapper around scripts/*.sh — all build logic lives in scripts.
#
# Docker-first: lint, test, build, and publish all run in containers.
#
# Release workflow:
#   make release VERSION=v0.1.0    # tag, images, chart, GitHub release — one command

.PHONY: lint lint-python \
	test test-python \
	build images \
	pentest pentest-sast pentest-images pentest-dast \
	release publish publish-images publish-chart publish-release \
	dev dev-down \
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

# ── Security Testing ────────────────────────────────────────
pentest:            ## Run all pen tests (SAST + images + DAST)
	scripts/pentest.sh

pentest-sast:       ## Run SAST analysis (Semgrep)
	scripts/pentest.sh sast

pentest-images:     ## Scan container images for CVEs (Trivy)
	scripts/pentest.sh images

pentest-dast:       ## Run DAST analysis (Nuclei, requires running stack)
	scripts/pentest.sh dast

# ── Release ──────────────────────────────────────────────
release:            ## Full release: tag + images + chart + GitHub release (VERSION required)
	@if [ -z "$(VERSION)" ]; then \
		echo "\033[1;31m==> ERROR: VERSION is required.\033[0m"; \
		echo "Usage: make release VERSION=v0.1.0"; \
		exit 1; \
	fi
	VERSION=$(VERSION) scripts/publish.sh

# ── Publish (individual targets) ─────────────────────────
publish:            ## Publish images + chart (VERSION required)
	VERSION=$(VERSION) scripts/publish.sh images
	VERSION=$(VERSION) scripts/publish.sh chart

publish-images:     ## Push multi-arch images to GHCR (VERSION required)
	VERSION=$(VERSION) scripts/publish.sh images

publish-chart:      ## Push Helm chart to OCI registry (VERSION required)
	VERSION=$(VERSION) scripts/publish.sh chart

publish-release:    ## Create GitHub Release (VERSION required)
	VERSION=$(VERSION) scripts/publish.sh release

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
