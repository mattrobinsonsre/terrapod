#!/usr/bin/env bash
# Build release-shaped, signed provider artifacts for local Tilt development.
set -euo pipefail

PROVIDER_DIR=$(dirname "$(realpath "$0")")
REPO_DIR=$(dirname "$PROVIDER_DIR")
STORE="$PROVIDER_DIR/.dev-build"
KEY_HOME="$HOME/.local/share/terrapod/certs/provider-gnupg"
OLD_KEY_HOME="$STORE/gnupg"
KEY_ID='Terrapod local dev provider <dev@terrapod.local>'

die() { printf '%s\n' "$*" >&2; exit 1; }

artifact_version() {
  local base=$1 digest suffix
  [[ "$base" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Expected numeric base version, got: $base"
  digest=$(
    cd "$REPO_DIR"
    git ls-files --cached --others --exclude-standard -z -- \
      provider/GNUmakefile provider/dev-package.sh provider/go.mod provider/go.sum \
      provider/main.go provider/internal go-terrapod \
      | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum
  )
  suffix=$((16#${digest:0:10}))
  printf '%s-dev.%s\n' "$base" "$suffix"
}

ensure_key() {
  install -d -m 0700 "$STORE"
  if [ -L "$KEY_HOME" ] || [ -L "$OLD_KEY_HOME" ]; then
    die 'Refusing to use a symlink for the dev signing key'
  fi
  if [ ! -e "$KEY_HOME" ] && [ -d "$OLD_KEY_HOME" ]; then
    if [ -s "$STORE/signing-key.asc" ] && \
        ! gpg --homedir "$OLD_KEY_HOME" --armor --export "$KEY_ID" \
          | cmp -s "$STORE/signing-key.asc" -; then
      die 'Existing dev signing key does not match the exported public key'
    fi
    mkdir -p "$(dirname "$KEY_HOME")"
    mv "$OLD_KEY_HOME" "$KEY_HOME"
  fi
  install -d -m 0700 "$KEY_HOME"
  if ! gpg --homedir "$KEY_HOME" --list-secret-keys --with-colons "$KEY_ID" 2>/dev/null \
      | grep -q '^sec:'; then
    if [ -s "$STORE/signing-key.asc" ] || [ -d "$STORE/artifacts" ]; then
      die 'Existing signed artifacts require the original dev signing key'
    fi
    gpg --homedir "$KEY_HOME" --batch --pinentry-mode loopback --passphrase '' \
      --quick-generate-key "$KEY_ID" ed25519 sign 0 >&2
  fi
  if [ -s "$STORE/signing-key.asc" ] && \
      ! gpg --homedir "$KEY_HOME" --armor --export "$KEY_ID" \
        | cmp -s "$STORE/signing-key.asc" -; then
    die 'Dev signing key changed; refusing to replace the exported public key'
  fi
  gpg --homedir "$KEY_HOME" --armor --export "$KEY_ID" > "$STORE/signing-key.asc"
  test -s "$STORE/signing-key.asc" || die 'Dev signing public key is empty'
}

bundle() {
  local version=$1 binary=$2 output="$STORE/artifacts/$1" staging inner archive sums sig
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$ ]] || die "Invalid dev version: $version"
  test -s "$binary" || die "Provider binary missing: $binary"
  ensure_key
  inner="terraform-provider-terrapod_v$version"
  archive="terraform-provider-terrapod_${version}_linux_$(go env GOARCH).zip"
  sums="terraform-provider-terrapod_${version}_SHA256SUMS"
  sig="$sums.sig"
  if [ -e "$output" ]; then
    test -s "$output/$archive" && test -s "$output/$sums" && test -s "$output/$sig" \
      || die "Incomplete artifact set for $version; refuse to overwrite"
    (cd "$output" && sha256sum -c "$sums")
    gpg --homedir "$KEY_HOME" --verify "$output/$sig" "$output/$sums" >&2
    cmp -s "$binary" <(unzip -p "$output/$archive" "$inner") \
      || die "Binary differs from existing $version; choose a new dev version"
    printf '%s\n' "$version" > "$STORE/current-version"
    return
  fi

  install -d -m 0700 "$STORE/artifacts"
  staging=$(mktemp -d "$STORE/artifacts/.staging.XXXXXXXX")
  trap 'rm -rf "$staging"' EXIT
  install -m 0755 "$binary" "$staging/$inner"
  (cd "$staging" && zip -q -X "$archive" "$inner" && sha256sum "$archive" > "$sums")
  gpg --homedir "$KEY_HOME" --batch --local-user "$KEY_ID" \
    --output "$staging/$sig" --detach-sign "$staging/$sums"
  (cd "$staging" && sha256sum -c "$sums")
  gpg --homedir "$KEY_HOME" --verify "$staging/$sig" "$staging/$sums" >&2
  mv "$staging" "$output"
  trap - EXIT
  printf '%s\n' "$version" > "$STORE/current-version"
  printf 'Signed local provider %s: %s\n' "$version" "$output"
}

generate_test_module() {
  local version source output file staging
  version=$(<"$STORE/current-version")
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+-dev\.[0-9]+$ ]] \
    || die "Invalid current dev provider version: $version"
  source="$PROVIDER_DIR/examples/modules/terrapod_self_contained/main.tf"
  output="$STORE/terrapod_self_contained-test"
  test -f "$source" && test ! -L "$source" || die "Missing or linked module source: $source"
  if [ -L "$output" ] || { [ -e "$output" ] && [ ! -d "$output" ]; }; then
    die "Refusing to replace test module directory: $output"
  fi
  install -d "$output"
  for file in "$output"/* "$output"/.[!.]* "$output"/..?*; do
    if [ -e "$file" ] || [ -L "$file" ]; then
      [[ "$file" == "$output/main.tf" && ! -L "$file" && -f "$file" ]] \
        || die "Unexpected file in test module directory: $file"
    fi
  done
  staging=$(mktemp "$output/.main.tf.XXXXXXXX")
  if ! awk -v version="$version" '
    /^[[:space:]]*source[[:space:]]*=[[:space:]]*"terrapod.local\/default\/terrapod"[[:space:]]*$/ {
      count++
      print "      source  = \"terrapod.local/default/terrapod\""
      print "      version = \"" version "\""
      next
    }
    /^[[:space:]]*version[[:space:]]*=/ { invalid = 1 }
    { print }
    END { if (count != 1 || invalid) exit 1 }
  ' "$source" > "$staging"; then
    rm -f "$staging"
    die "Expected one unpinned Terrapod provider in $source"
  fi
  mv -f "$staging" "$output/main.tf"
  printf 'Upload-ready test module for %s: %s\n' "$version" "$output"
}

case "${1:-}" in
  version)
    artifact_version "${2:?usage: dev-package.sh version <base-version>}"
    ;;
  build)
    base=${2:?usage: dev-package.sh build <base-version>}
    version=$(artifact_version "$base")
    GOOS=linux CGO_ENABLED=0 make -C "$PROVIDER_DIR" build VERSION="$version"
    bundle "$version" "$PROVIDER_DIR/terraform-provider-terrapod"
    generate_test_module
    ;;
  *) die 'usage: dev-package.sh build <base-version>' ;;
esac
