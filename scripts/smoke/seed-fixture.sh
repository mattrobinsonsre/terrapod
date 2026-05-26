#!/usr/bin/env bash
# Generates a faux Atlantis-style local clone for the smoke test.
#
# The fixture is a one-project repo:
#   <fixture>/atlantis.yaml             — atlantis v3 config
#   <fixture>/example/main.tf           — null_resource + S3 backend
#   <fixture>/.git                      — initialised + a fake origin URL
#
# After `terraform apply` is run in <fixture>/example, the state lives
# in minio (s3://tfstate/example/terraform.tfstate) and the migrator
# can read it via the same AWS SDK code path that real S3 uses.
#
# Usage: scripts/smoke/seed-fixture.sh <dest-dir>
#
# Side-effects: creates / overwrites <dest-dir>. Idempotent — running
# twice produces the same layout.

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <dest-dir>" >&2
  exit 2
fi

DEST=$1
mkdir -p "$DEST/example"

cat > "$DEST/atlantis.yaml" <<'YAML'
version: 3
projects:
  - name: example
    dir: example
    branch: /main/
YAML

cat > "$DEST/example/main.tf" <<'HCL'
terraform {
  required_version = ">= 1.5"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
  backend "s3" {
    bucket = "tfstate"
    key    = "example/terraform.tfstate"
    region = "us-east-1"
  }
}

resource "null_resource" "smoke" {
  triggers = {
    hostname = "terrapod-smoke"
  }
}

output "smoke_id" {
  value = null_resource.smoke.id
}
HCL

# A real local clone needs a git remote URL so terrapod-migrate can
# derive RepoURL. We don't actually push anywhere — the URL is just
# metadata the migrator stamps onto the workspace's vcs_repo_url.
cd "$DEST"
if [ ! -d .git ]; then
  git init -b main >/dev/null
fi
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/mattrobinsonsre/terrapod-smoke.git"
git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main 2>/dev/null || \
  echo "ref: refs/remotes/origin/main" > .git/refs/remotes/origin/HEAD 2>/dev/null || true

echo "Fixture seeded at $DEST"
echo "  atlantis.yaml + example/ project with null_resource + S3 backend"
echo "  remote.origin.url = https://github.com/mattrobinsonsre/terrapod-smoke.git"
