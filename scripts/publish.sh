#!/usr/bin/env bash
# Push images and Helm chart, create GitHub Release.
#
# Usage:
#   scripts/publish.sh [tag|images|chart|release|all]
#
#   tag      Create + push git tag (VERSION must be set)
#   images   Build + push multi-arch Docker images to GHCR
#   chart    Package + push Helm chart to OCI registry
#   release  Create GitHub Release with auto-generated notes
#   all      tag + images + chart + release (default)
#
# VERSION is required for all targets. Set via:
#   VERSION=v0.1.0 scripts/publish.sh
#   make release VERSION=v0.1.0
#
# Prerequisites:
#   images:  docker login ghcr.io
#   chart:   helm registry login ghcr.io
#   release: gh auth status

set -euo pipefail
source "$(dirname "$0")/lib.sh"

HELM_CHART_DIR="$REPO_ROOT/helm/terrapod"

# ── Create + push git tag ───────────────────────────────────
publish_tag() {
  require_clean_worktree

  if git -C "$REPO_ROOT" rev-parse "$VERSION" &>/dev/null 2>&1; then
    local existing_sha
    existing_sha=$(git -C "$REPO_ROOT" rev-parse "$VERSION")
    local head_sha
    head_sha=$(git -C "$REPO_ROOT" rev-parse HEAD)

    if [[ "$existing_sha" == "$head_sha" ]]; then
      info "Tag ${VERSION} already exists at HEAD — skipping"
      return 0
    else
      info "Tag ${VERSION} exists at ${existing_sha:0:7} but HEAD is ${head_sha:0:7}"
      info "Moving tag to HEAD..."
      git -C "$REPO_ROOT" tag -f "$VERSION" HEAD
      git -C "$REPO_ROOT" push origin "$VERSION" --force
    fi
  else
    info "Creating tag ${VERSION} at HEAD..."
    git -C "$REPO_ROOT" tag "$VERSION" HEAD
    git -C "$REPO_ROOT" push origin "$VERSION"
  fi

  success "Tag ${VERSION} points at $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
}

# ── Push multi-arch Docker images to GHCR ─────────────────
publish_images() {
  require_publish_prereqs images
  info "Publishing multi-arch Docker images to ${REGISTRY}..."

  local images=(terrapod-api terrapod-web terrapod-runner)
  local dockerfiles=(Dockerfile.api Dockerfile.web Dockerfile.runner)

  for i in "${!images[@]}"; do
    local name="${images[$i]}"
    local dockerfile="${dockerfiles[$i]}"

    local tags="-t ${REGISTRY}/${name}:${VERSION}"
    # Tag :latest only for semver tags (vX.Y.Z), not pre-releases
    if [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      tags="$tags -t ${REGISTRY}/${name}:latest"
    fi

    info "  ${name}..."
    docker buildx build -f "$REPO_ROOT/docker/${dockerfile}" \
      --platform linux/amd64,linux/arm64 \
      $tags --push "$REPO_ROOT"
  done

  success "Multi-arch images pushed to ${REGISTRY}"
}

# ── Push Helm chart to OCI registry ───────────────────────
publish_chart() {
  require_publish_prereqs chart
  info "Publishing Helm chart to OCI..."

  # Strip leading 'v' from version for Helm (semver without prefix)
  local chart_version="${VERSION#v}"

  mkdir -p "$REPO_ROOT/dist"

  # Package the chart with version and appVersion from git tag
  helm package "$HELM_CHART_DIR" --destination "$REPO_ROOT/dist/" \
    --version "$chart_version" --app-version "$chart_version"

  # Push to GHCR OCI
  helm push "$REPO_ROOT/dist/terrapod-${chart_version}.tgz" "oci://${REGISTRY}"

  success "Helm chart terrapod:${chart_version} pushed to oci://${REGISTRY}"
}

# ── Generate release notes from conventional commits ─────
generate_release_notes() {
  # Find the previous tag to diff against
  local prev_tag
  prev_tag=$(git -C "$REPO_ROOT" describe --tags --abbrev=0 "${VERSION}^" 2>/dev/null || echo "")

  local range
  if [[ -n "$prev_tag" ]]; then
    range="${prev_tag}..${VERSION}"
  else
    range="$VERSION"
  fi

  # Collect commits by category using conventional commit prefixes.
  local feats="" fixes="" docs="" refactors="" tests="" chores="" others=""

  while IFS= read -r line; do
    # Strip conventional commit prefix: "type: msg" or "type(scope): msg"
    local msg
    msg=$(printf '%s' "$line" | gsed -E 's/^[a-z]+(\([^)]*\))?[!]?:[[:space:]]*//')

    case "$line" in
      feat:*|feat\(*)     feats+="- ${msg}"$'\n' ;;
      fix:*|fix\(*)       fixes+="- ${msg}"$'\n' ;;
      docs:*|docs\(*)     docs+="- ${msg}"$'\n' ;;
      refactor:*|refactor\(*) refactors+="- ${msg}"$'\n' ;;
      test:*|test\(*)     tests+="- ${msg}"$'\n' ;;
      chore:*|chore\(*|ci:*|ci\(*) chores+="- ${msg}"$'\n' ;;
      *)                  others+="- ${line}"$'\n' ;;
    esac
  done < <(git -C "$REPO_ROOT" log --format='%s' "$range" 2>/dev/null)

  # Build the notes body
  local notes=""

  if [[ -n "$feats" ]]; then
    notes+="### Features"$'\n\n'"$feats"$'\n'
  fi
  if [[ -n "$fixes" ]]; then
    notes+="### Bug Fixes"$'\n\n'"$fixes"$'\n'
  fi
  if [[ -n "$docs" ]]; then
    notes+="### Documentation"$'\n\n'"$docs"$'\n'
  fi
  if [[ -n "$refactors" ]]; then
    notes+="### Refactoring"$'\n\n'"$refactors"$'\n'
  fi
  if [[ -n "$tests" ]]; then
    notes+="### Tests"$'\n\n'"$tests"$'\n'
  fi
  if [[ -n "$chores" ]]; then
    notes+="### Maintenance"$'\n\n'"$chores"$'\n'
  fi
  if [[ -n "$others" ]]; then
    notes+="### Other Changes"$'\n\n'"$others"$'\n'
  fi

  if [[ -n "$prev_tag" ]]; then
    notes+="**Full Changelog**: https://github.com/mattrobinsonsre/terrapod/compare/${prev_tag}...${VERSION}"$'\n'
  fi

  printf '%s' "$notes"
}

# ── Create GitHub Release ─────────────────────────────────
publish_release() {
  require_publish_prereqs release
  info "Creating GitHub Release ${VERSION}..."

  mkdir -p "$REPO_ROOT/dist"

  info "Generating release notes..."
  local notes
  notes=$(generate_release_notes)

  # Include Helm chart package if it exists
  local assets=()
  for f in "$REPO_ROOT"/dist/*.tgz; do
    [[ -f "$f" ]] && assets+=("$f")
  done

  if [[ ${#assets[@]} -gt 0 ]]; then
    # Generate checksums
    local sha_cmd="sha256sum"
    command -v gsha256sum &>/dev/null && sha_cmd="gsha256sum"
    info "Generating checksums..."
    (cd "$REPO_ROOT/dist" && $sha_cmd -- *.tgz > checksums.txt)
    assets+=("$REPO_ROOT/dist/checksums.txt")

    gh release create "$VERSION" "${assets[@]}" \
      --title "Terrapod ${VERSION}" \
      --notes "$notes"
  else
    gh release create "$VERSION" \
      --title "Terrapod ${VERSION}" \
      --notes "$notes"
  fi

  success "GitHub Release ${VERSION} created"
}

# ── Main ──────────────────────────────────────────────────
target="${1:-all}"

# All targets except 'tag' require a valid semver version
if [[ "$target" != "tag" ]]; then
  require_semver_version
fi

case "$target" in
  tag)
    require_semver_version
    publish_tag
    ;;
  images)  publish_images ;;
  chart)   publish_chart ;;
  release) publish_release ;;
  all)
    publish_tag
    publish_images
    publish_chart
    publish_release
    success "All artifacts published for ${VERSION}"
    ;;
  *)
    error "Unknown target: $target"
    echo "Usage: $0 [tag|images|chart|release|all]"
    echo ""
    echo "Targets:"
    echo "  tag      Create + push git tag"
    echo "  images   Build + push multi-arch Docker images to GHCR"
    echo "  chart    Package + push Helm chart to OCI registry"
    echo "  release  Create GitHub Release with auto-generated notes"
    echo "  all      tag + images + chart + release (default)"
    echo ""
    echo "VERSION must be set (e.g. VERSION=v0.1.0)"
    exit 1
    ;;
esac
