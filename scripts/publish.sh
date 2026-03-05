#!/usr/bin/env bash
# Push images and Helm chart, create GitHub Release.
# Usage: scripts/publish.sh [images|chart|release|all]
# Default: all
#
# Prerequisites:
#   images:  docker login ghcr.io
#   chart:   helm registry login ghcr.io
#   release: gh auth status

set -euo pipefail
source "$(dirname "$0")/lib.sh"

HELM_CHART_DIR="$REPO_ROOT/helm/terrapod"

# ── Push multi-arch Docker images to GHCR ─────────────────
publish_images() {
  info "Publishing multi-arch Docker images to ${REGISTRY}..."

  local images=(terrapod-api terrapod-web terrapod-runner)
  local dockerfiles=(Dockerfile.api Dockerfile.web Dockerfile.runner)

  for i in "${!images[@]}"; do
    local name="${images[$i]}"
    local dockerfile="${dockerfiles[$i]}"

    local tags="-t ${REGISTRY}/${name}:${VERSION}"
    # Tag :latest only for semver tags (vX.Y.Z)
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

target="${1:-all}"

case "$target" in
  images)  publish_images ;;
  chart)   publish_chart ;;
  release) publish_release ;;
  all)
    publish_images
    publish_chart
    publish_release
    success "All artifacts published"
    ;;
  *)
    error "Unknown target: $target"
    echo "Usage: $0 [images|chart|release|all]"
    exit 1
    ;;
esac
