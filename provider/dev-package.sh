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

# A released base version, which may carry a pre-release: `2.0.0` or
# `2.0.0-alpha.1`. The alpha form is not hypothetical -- the Tiltfile derives the
# dev provider version from the newest `v[0-9]*` tag, so the day `v2.0.0-alpha.1`
# was published the whole LOCAL DEV STACK stopped starting: `dev-package.sh`
# refused the version, the Tiltfile could not evaluate, and Tilt therefore built
# nothing at all while the already-running API went on answering 200 -- which
# reads as a wedged stack rather than as a version-string refusal.
BASE_VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'
#: And the version this script DERIVES from it. A plain base gains a `-dev.N`
#: pre-release; a base that already has one gains `.dev.N` INSIDE it, so there is
#: ever only one pre-release section. `2.0.0-alpha.1-dev.7` would be legal semver
#: by the letter -- a hyphen is allowed inside a pre-release identifier -- and is
#: ambiguous to read and sorts oddly, where `2.0.0-alpha.1.dev.7` plainly follows
#: `2.0.0-alpha.1`.
DEV_VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+-([0-9A-Za-z.-]+\.)?dev\.[0-9]+$'

artifact_version() {
  local base=$1 digest suffix goversion
  [[ "$base" =~ $BASE_VERSION_RE ]] || die "Expected a semver base version, got: $base"
  # The Go toolchain is an INPUT to the compiled bytes, and the one input the file
  # hashes cannot see. `provider/go.mod` IS in the digest below, but its `go`
  # directive is a MINIMUM rather than a pin -- so a host upgrade inside the
  # declared range changes the binary and nothing else. The version then does not
  # move, `bundle()` rightly refuses to overwrite a stored artifact whose bytes
  # differ, and the local dev stack stops building with a refusal that names
  # neither the toolchain nor the cause (#2461).
  #
  # Resolved BEFORE the digest subshell deliberately: inside it a `die` would end
  # only the subshell, leaving `digest` empty or truncated and yielding a silently
  # WRONG version -- worse than the refusal this fixes. It is also a non-file
  # input, so it is folded into the pipeline alongside the per-file hashes rather
  # than added to the path list.
  #
  # `go` is required by this script's own `build` path in any case (`bundle` reads
  # `go env GOARCH`, and `make build` compiles), so this moves the failure earlier
  # and makes it name its cause; it adds no new prerequisite. The Tiltfile calls
  # `version` while EVALUATING, and a Tiltfile that cannot evaluate builds nothing
  # while an already-running API goes on answering 200 -- which reads as a wedged
  # stack -- hence the explicit message rather than a bare `go: not found`.
  command -v go >/dev/null || die 'go is not on PATH, and the Go toolchain is part of the dev provider version. Install Go; the dev stack needs it to build the provider regardless.'
  goversion=$(go env GOVERSION) || die 'Could not read `go env GOVERSION`; cannot derive the dev provider version'
  [ -n "$goversion" ] || die '`go env GOVERSION` was empty; cannot derive the dev provider version'
  digest=$(
    cd "$REPO_DIR"
    {
      printf 'GOVERSION=%s\n' "$goversion"
      git ls-files --cached --others --exclude-standard -z -- \
        provider/GNUmakefile provider/dev-package.sh provider/go.mod provider/go.sum \
        provider/main.go provider/internal go-terrapod \
        | LC_ALL=C sort -z | xargs -0 sha256sum
    } | sha256sum
  )
  suffix=$((16#${digest:0:10}))
  # Extend the existing pre-release rather than starting a second one.
  if [[ "$base" == *-* ]]; then
    printf '%s.dev.%s\n' "$base" "$suffix"
  else
    printf '%s-dev.%s\n' "$base" "$suffix"
  fi
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
  [[ "$version" =~ $DEV_VERSION_RE ]] || die "Invalid dev version: $version"
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
    # Deliberately a hard stop rather than an overwrite: one version, one binary.
    # Letting this overwrite would trade a loud refusal for a cache that quietly
    # serves a binary other than the one its version names. When it fires, the
    # answer is to find the input that moved and is not in the digest -- the Go
    # toolchain was exactly that until #2461 put it there.
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
  [[ "$version" =~ $DEV_VERSION_RE ]] \
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
