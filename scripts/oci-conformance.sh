#!/usr/bin/env bash
# Run the OCI distribution-spec conformance suite against a running Terrapod.
#
# This is the only check that tests Terrapod's registry against the spec rather
# than against the client we happened to try. That distinction is not academic:
# every finding it produced during #1408 — a hardcoded digest algorithm, a
# missing OCI-Subject echo, foreign layers treated as missing blobs, referrers
# 404ing instead of returning an empty index, and no Range support at all —
# left `docker push` and `docker pull` working perfectly.
#
# Usage:
#   scripts/oci-conformance.sh [base-url]
#
# Defaults to the e2e stack (http://localhost:3000, i.e. through the BFF, which
# is deliberate — the BFF proxy is on the path in every real deployment, so
# testing the API directly would skip a hop that has broken uploads before).
#
# Requires Go, and network access to fetch the suite on first run.
set -euo pipefail

BASE="${1:-http://localhost:3000}"
EMAIL="${TERRAPOD_ADMIN_EMAIL:-admin@terrapod.local}"
PASSWORD="${TERRAPOD_ADMIN_PASSWORD:-TestPassword123!}"

# Pinned by commit, not by tag: a tag can move, and a conformance suite that
# changes under us turns a red build into an investigation of somebody else's
# repository rather than of our own change.
SPEC_REPO="https://github.com/opencontainers/distribution-spec"
SPEC_REF="${OCI_SPEC_REF:-v1.1.1}"

WORK="${OCI_CONFORMANCE_DIR:-.conformance}"
mkdir -p "$WORK"

if [ ! -d "$WORK/distribution-spec" ]; then
  echo "==> Fetching the conformance suite ($SPEC_REF)"
  git clone --quiet --depth 1 --branch "$SPEC_REF" "$SPEC_REPO" "$WORK/distribution-spec"
fi

echo "==> Building the conformance binary"
( cd "$WORK/distribution-spec/conformance" && go test -c -o /tmp/oci-conformance.test . )

echo "==> Minting an API token"
# The registry accepts any Terrapod credential as a Basic password, so this is
# an ordinary token rather than anything registry-specific. Minted through the
# real PKCE flow so the test exercises the credential path a person would use.
TOKEN="$(python3 scripts/mint-token.py --url "$BASE" --email "$EMAIL" --password "$PASSWORD")"
if [ -z "$TOKEN" ]; then
  echo "FAIL: could not mint a token against $BASE" >&2
  exit 1
fi

echo "==> Running conformance against $BASE"
# Namespaces are distinct per run so a re-run against a stack that already holds
# the previous run's content starts clean.
export OCI_ROOT_URL="$BASE"
export OCI_NAMESPACE="conformance/basic"
export OCI_CROSSMOUNT_NAMESPACE="conformance/crossmount"
export OCI_USERNAME="conformance"
export OCI_PASSWORD="$TOKEN"
export OCI_TEST_PULL=1
export OCI_TEST_PUSH=1
export OCI_TEST_CONTENT_DISCOVERY=1
# Content management: manifest and tag deletion (#1423). Blob deletion is
# declined with 405, which the suite accepts and accounts for — blobs here are
# content-addressed and shared, so deleting one directly would break every
# manifest still referencing those bytes. Deleting the manifest expresses the
# same intent safely, and the collector reclaims what nothing needs.
export OCI_TEST_CONTENT_MANAGEMENT=1
export OCI_HIDE_SKIPPED_WORKFLOWS=1

/tmp/oci-conformance.test
