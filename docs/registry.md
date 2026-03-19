# Private Registry & Caching

Terrapod includes a private module and provider registry, plus pull-through caching for upstream public registries and terraform/tofu CLI binaries. This eliminates direct internet dependencies from runner Jobs.

---

## Overview

```
terraform init
    |
    +-- Module sources (e.g., "terrapod.local/default/vpc/aws")
    |       |
    |       +-- Terrapod Module Registry (private modules only)
    |
    +-- Provider sources (e.g., "hashicorp/aws")
            |
            +-- Network mirror (TF_CLI_CONFIG_FILE) --> Terrapod Provider Cache
                    |
                    +-- Cached? --> Serve from object storage
                    +-- Not cached? --> Fetch from registry.terraform.io, cache, serve
```

Both caching layers (providers and binaries) sit in front of the Terrapod API, so runner Jobs have zero direct upstream dependencies.

---

## Private Module Registry

Publish, version, and share Terraform modules internally.

![Module Registry](images/registry-modules.png)

### Creating a Module

```zsh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/registry-modules \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-modules",
      "attributes": {
        "name": "vpc",
        "provider": "aws"
      }
    }
  }'
```

### Creating a Version

```zsh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/registry-modules/private/default/vpc/aws/versions \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-module-versions",
      "attributes": {
        "version": "1.0.0"
      }
    }
  }'
```

The response includes a presigned `upload-url`. Upload the module tarball:

```zsh
# Create tarball from module directory
tar -czf module.tar.gz -C /path/to/module .

# Upload to presigned URL
curl -X PUT "<upload-url>" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @module.tar.gz
```

### Using a Private Module

In your Terraform configuration:

```hcl
module "vpc" {
  source  = "terrapod.example.com/default/vpc/aws"
  version = "1.0.0"

  # module variables...
}
```

The Terraform CLI discovers the module registry via `/.well-known/terraform.json` which includes `modules.v1` pointing to the registry endpoint.

### Listing Modules

```zsh
curl https://terrapod.example.com/api/v2/organizations/default/registry-modules \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Module Versions

```zsh
# List versions (CLI protocol)
curl https://terrapod.example.com/api/v2/registry/modules/default/vpc/aws/versions \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"

# Show module details (TFE V2 API)
curl https://terrapod.example.com/api/v2/organizations/default/registry-modules/private/default/vpc/aws \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Deleting a Module

```zsh
curl -X DELETE https://terrapod.example.com/api/v2/organizations/default/registry-modules/private/default/vpc/aws \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Storage Layout

Module tarballs are stored at:

```
registry/modules/{namespace}/{name}/{provider}/{version}.tar.gz
```

### RBAC

Modules follow the same owner + label RBAC model as workspaces:
- Creator becomes owner (admin permission)
- Label-based roles grant read/write/admin
- The `workspace_permission` on a role maps to registry permissions: `plan` maps to `read`
- Runner tokens receive implicit `read` access (required for `terraform init` to download modules)

Module API responses include a `permissions` block (`can-update`, `can-destroy`, `can-create-version`) reflecting the caller's effective access. The web UI uses these to gate editing, deletion, and version upload controls.

**Self-lockout protection:** If a label change on a module or provider would reduce the caller's own access, the API returns 409 Conflict. The UI shows a warning banner with Revert / Save Anyway options. See the [RBAC docs](rbac.md#self-lockout-protection-on-label-changes) for details.

---

## VCS-Driven Module Publishing

Instead of uploading tarballs manually, you can connect a module to a VCS repository. Terrapod watches for new git tags and automatically publishes matching versions.

### Overview

1. Create a module in the registry
2. Connect it to a VCS repository (GitHub or GitLab) via the UI or API
3. Push semver tags (e.g. `v1.0.0`) to the repository
4. Terrapod's background poller detects the tag, downloads the archive, and creates the module version

> **Important:** Only git tags trigger version publishing. Commits pushed to branches — including the default branch — do **not** create module versions. You must push a tag matching the configured tag pattern (default `v*`) for a new version to appear in the registry. This matches the Terraform registry convention where module versions correspond to git tags, not branch HEADs.
>
> If you want every merge to `main` to produce a new version automatically, set up a CI pipeline on the module repository that creates a semver tag on each merge (see example below).

### Setup via API

```zsh
# 1. Create the module
curl -X POST https://terrapod.example.com/api/v2/organizations/default/registry-modules \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-modules",
      "attributes": { "name": "vpc", "provider": "aws" }
    }
  }'

# 2. Connect VCS
curl -X PATCH https://terrapod.example.com/api/v2/organizations/default/registry-modules/private/default/vpc/aws/vcs \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-modules",
      "attributes": {
        "source": "vcs",
        "vcs_connection_id": "<connection-id>",
        "vcs_repo_url": "https://github.com/my-org/terraform-aws-vpc",
        "vcs_tag_pattern": "v*"
      }
    }
  }'
```

You can also configure VCS from the module detail page in the web UI using the **Connect VCS** button.

### Tag Patterns

The `vcs_tag_pattern` field uses glob syntax to match tags and extract version strings:

| Pattern | Tag Example | Extracted Version |
|---|---|---|
| `v*` (default) | `v1.2.3` | `1.2.3` |
| `release-*` | `release-1.0.0` | `1.0.0` |
| `*` | `1.0.0` | `1.0.0` |

Only tags matching the pattern are considered. The prefix before the `*` wildcard is stripped to produce the version string.

### Polling Behaviour

- The registry VCS poller runs as a periodic task via the distributed scheduler (default: every 60 seconds, shared with the workspace VCS poller interval)
- Exactly one replica executes each poll cycle (multi-replica safe via Redis)
- Each cycle queries all modules with `source=vcs` and a configured VCS connection, then lists tags from the VCS provider

### Git as Source of Truth

**Published versions track git, not the other way around.** If a tag is moved to a different commit (e.g. via `git tag -f v1.0.0 <new-sha> && git push --force --tags`), Terrapod detects the SHA mismatch on the next poll cycle, re-downloads the archive, and replaces the stored tarball. The `vcs-commit-sha` on the version record is updated to reflect the new commit.

This means:
- Versions are **not** immutable when backed by VCS
- The registry always reflects the current state of git tags
- Each version tracks which commit SHA and tag name produced it

### What Gets Stored

The archive at the tag ref is stored as a tarball at:

```
registry/modules/{namespace}/{name}/{provider}/{version}.tar.gz
```

### Manual Upload Still Works

Even with VCS connected, you can still upload versions directly via the API or web UI. Manually-uploaded versions will have empty `vcs-commit-sha` and `vcs-tag` fields.

### Auto-Tagging via CI

Since only git tags produce module versions, you may want a CI pipeline that automatically creates a semver tag when code is merged to the default branch. Here is a minimal GitHub Actions example that bumps the patch version on every merge:

```yaml
# .github/workflows/tag.yml
name: Auto-tag on merge
on:
  push:
    branches: [main]

permissions:
  contents: write

jobs:
  tag:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Get latest tag
        id: latest
        run: |
          tag=$(git tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
          echo "tag=${tag:-v0.0.0}" >> "$GITHUB_OUTPUT"
      - name: Bump patch version
        id: bump
        run: |
          ver="${{ steps.latest.outputs.tag }}"
          ver="${ver#v}"
          IFS='.' read -r major minor patch <<< "$ver"
          echo "new_tag=v${major}.${minor}.$((patch + 1))" >> "$GITHUB_OUTPUT"
      - name: Create tag
        run: |
          git tag "${{ steps.bump.outputs.new_tag }}"
          git push origin "${{ steps.bump.outputs.new_tag }}"
```

With this in place, every merge to `main` creates a new patch version tag (e.g. `v0.0.1` → `v0.0.2`), which Terrapod's poller picks up and publishes as a new module version within 60 seconds.

### Version Metadata

Each version exposes VCS metadata in the API response:

| Field | Description |
|---|---|
| `vcs-commit-sha` | The git commit SHA this version was built from (empty for manual uploads) |
| `vcs-tag` | The tag name that matched (e.g. `v1.2.3`; empty for manual uploads) |

---

## Module Impact Analysis

When a PR is opened against a VCS-connected module, Terrapod can automatically generate **speculative (plan-only) runs** on linked workspaces to show the impact of the module change before it's merged. This is a novel feature with no equivalent in TFE/TFC.

### How It Works

1. An admin **links workspaces** to a module — these are workspaces that consume the module
2. When a PR is opened or updated on the module's VCS repo, Terrapod's background poller detects it
3. For each linked workspace, Terrapod creates a **plan-only run** that uses the PR branch code instead of the published module version
4. The plan results are posted back to the PR as commit statuses and comments

The key insight: the module download endpoint is intercepted at the registry level. When the runner executes `terraform init`, it transparently receives the PR branch tarball instead of the published version — no modification of Terraform code required.

### Linking Workspaces

Link workspaces to a module via the API or the web UI (on the module detail page):

```zsh
# Link a workspace
curl -X POST https://terrapod.example.com/api/v2/organizations/default/registry-modules/private/default/vpc/aws/workspace-links \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspace-links",
      "attributes": {
        "workspace_id": "<workspace-id>"
      }
    }
  }'

# List linked workspaces
curl https://terrapod.example.com/api/v2/organizations/default/registry-modules/private/default/vpc/aws/workspace-links \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"

# Remove a link
curl -X DELETE https://terrapod.example.com/api/v2/organizations/default/registry-modules/private/default/vpc/aws/workspace-links/<link-id> \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

Linking requires **admin** permission on the module.

### Module Override Mechanism

Each speculative run carries a `module_overrides` field — a JSON map of module coordinates to override storage paths:

```json
{
  "default/vpc/aws": "module_overrides/abc123def/default/vpc/aws.tar.gz"
}
```

When the runner downloads the module during `terraform init`, the download endpoint checks the run's overrides and serves the PR tarball instead of the published version. Override tarballs are keyed by commit SHA, so retries and multiple linked workspaces share the same tarball.

### PR Polling and Deduplication

- The module impact poller runs as a periodic task alongside the registry VCS poller (same interval)
- For each VCS-connected module with workspace links, it lists open PRs targeting the default branch
- New commits on a PR trigger new speculative runs; same SHA is skipped (deduplication via `vcs_last_pr_shas`)
- When a PR is closed or merged, active speculative runs for that PR are cancelled

### Automatic Runs on Version Publish

When a new module version is published — either via manual upload or VCS tag auto-publish — Terrapod automatically queues **standard runs** (not plan-only) on all linked workspaces. This ensures consuming workspaces are updated when a new module version becomes available.

### Run Sources

| Source | Trigger | Run Type |
|---|---|---|
| `module-test` | PR opened/updated on module repo | Plan-only (speculative) |
| `module-publish` | New module version published | Standard (plan + apply) |

### VCS Status Reporting

For `module-test` runs, Terrapod posts commit statuses and PR comments to the module's VCS repository:
- **Pending** status when the run starts
- **Success/failure** status when the run completes
- PR comment with a link to the run detail page and plan summary

### Requirements

- Module must be VCS-connected (source = `vcs` with a configured VCS connection)
- At least one workspace must be linked to the module
- The VCS connection must have permissions to list PRs and download archives

### Limitations

- Only works with VCS-connected modules (manual-upload modules don't have a repo to poll)
- PR polling uses the same interval as the workspace VCS poller (default 60 seconds)
- Override mechanism works at the module level — if a workspace uses multiple modules from the same PR, each needs its own link

---

## Private Provider Registry

Publish, version, and share Terraform providers internally with GPG signing.

![Provider Registry](images/registry-providers.png)

### GPG Key Management

Before publishing providers, register a GPG key for signature verification:

```zsh
# Export your GPG public key
gpg --armor --export your-key-id > public-key.asc

# Register with Terrapod
curl -X POST https://terrapod.example.com/api/registry/private/v2/gpg-keys \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d "{
    \"data\": {
      \"type\": \"gpg-keys\",
      \"attributes\": {
        \"namespace\": \"default\",
        \"ascii-armor\": \"$(cat public-key.asc)\"
      }
    }
  }"
```

The key ID is automatically extracted from the ASCII armor using `pgpy` (pure Python, no gpg binary needed on the server).

### Listing GPG Keys

```zsh
curl https://terrapod.example.com/api/registry/private/v2/gpg-keys \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Creating a Provider

```zsh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/registry-providers \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-providers",
      "attributes": {
        "name": "mycloud"
      }
    }
  }'
```

### Creating a Version

```zsh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/registry-providers/private/default/mycloud/versions \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-provider-versions",
      "attributes": {
        "version": "1.0.0",
        "key-id": "ABCDEF1234567890"
      }
    }
  }'
```

### Adding Platforms

For each OS/architecture combination:

```zsh
curl -X POST https://terrapod.example.com/api/v2/organizations/default/registry-providers/private/default/mycloud/versions/1.0.0/platforms \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-provider-platforms",
      "attributes": {
        "os": "linux",
        "arch": "amd64",
        "filename": "terraform-provider-mycloud_1.0.0_linux_amd64.zip",
        "shasum": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      }
    }
  }'
```

Upload the binary, SHA256SUMS, and SHA256SUMS.sig files to the presigned URLs returned in the response.

### Using a Private Provider

```hcl
terraform {
  required_providers {
    mycloud = {
      source  = "terrapod.example.com/default/mycloud"
      version = "1.0.0"
    }
  }
}
```

### Storage Layout

```
registry/providers/{namespace}/{name}/{version}/
  terraform-provider-{name}_{version}_{os}_{arch}.zip
  SHA256SUMS
  SHA256SUMS.sig
```

---

## Provider Caching (Network Mirror)

The provider cache implements the Terraform network mirror protocol. Runner Jobs are configured to use Terrapod as their provider mirror, so all provider downloads go through the cache.

### How It Works

1. Runner Job has a CLI config file with a `credentials` block and network mirror:
   ```hcl
   credentials "terrapod.example.com" {
     token = "<runner-token>"
   }
   provider_installation {
     network_mirror {
       url = "https://terrapod.example.com/v1/providers/"
     }
   }
   ```
2. Terraform requests provider binaries from the mirror URL (authenticated via credentials block)
3. Terrapod checks the cache (`cached_provider_packages` table)
4. Cache hit: redirect to presigned URL in object storage
5. Cache miss: fetch from upstream (`registry.terraform.io` or `registry.opentofu.org`), store, serve

**Note:** Provider mirror endpoints require authentication. Runner Jobs authenticate via the `credentials` block in the CLI config, which sends the runner token as a Bearer token to the Terrapod host.

### Configuration

```yaml
api:
  config:
    registry:
      provider_cache:
        enabled: true
        upstream_registries:
          - registry.terraform.io
          - registry.opentofu.org
        warm_on_first_request: true
```

### Network Mirror Endpoints

```
GET /v1/providers/{hostname}/{namespace}/{type}/index.json
```

Returns a version list for the provider.

```
GET /v1/providers/{hostname}/{namespace}/{type}/{version}.json
```

Returns platform-specific download URLs with `zh:` (zip hash) checksums.

### Runner Integration

Both `terraform` and `tofu` respect `TF_CLI_CONFIG_FILE`, so a single config file works for either backend. The listener injects this env var into runner Jobs automatically.

Runners should never fetch providers directly from upstream registries.

### Storage Layout

```
cache/providers/{hostname}/{namespace}/{type}/{version}/{filename}
```

---

## Binary Caching (Terraform/Tofu CLI)

The binary cache stores terraform and tofu CLI binaries so runner Jobs can fetch the exact version they need at startup, without downloading from the internet.

### How It Works

1. Runner Job starts with a generic image (no baked-in terraform/tofu binary)
2. Runner entrypoint calls `GET /api/v2/binary-cache/{tool}/{version}/{os}/{arch}` with auth header (`Authorization: Bearer <runner-token>`)
3. API validates authentication, returns 302 redirect to presigned URL in object storage
4. Cache miss: API fetches from upstream (`releases.hashicorp.com` for terraform, GitHub releases for tofu), stores, redirects
5. Runner downloads binary and begins execution

**Note:** The binary cache download endpoint requires authentication. Unauthenticated requests return 401.

### Configuration

```yaml
api:
  config:
    registry:
      binary_cache:
        enabled: true
        terraform_mirror_url: "https://releases.hashicorp.com/terraform"
        tofu_mirror_url: "https://github.com/opentofu/opentofu/releases/download"
```

### API Endpoints

**Download binary (used by runners):**
```
GET /api/v2/binary-cache/{tool}/{version}/{os}/{arch}
Authorization: Bearer <token>
```

Returns 302 redirect to presigned URL. Authentication required.

**List cached binaries (admin):**
```zsh
curl https://terrapod.example.com/api/v2/admin/binary-cache \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

**Pre-warm cache (admin):**
```zsh
curl -X POST https://terrapod.example.com/api/v2/admin/binary-cache/warm \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "terraform",
    "version": "1.9.8",
    "os": "linux",
    "arch": "amd64"
  }'
```

**Purge cached binary (admin):**
```zsh
curl -X DELETE https://terrapod.example.com/api/v2/admin/binary-cache/terraform/1.9.8 \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

### Storage Layout

```
cache/binaries/{tool}/{version}/{os}_{arch}
```

### Admin UI

The web UI includes a binary cache admin page at `/admin/binary-cache` (admin-only) for viewing, warming, and purging cached binaries.

![Binary Cache Admin](images/admin-binary-cache.png)

---

## Service Discovery

The `/.well-known/terraform.json` endpoint includes paths for both module and provider registries:

```json
{
  "modules.v1": "/api/v2/registry/modules/",
  "providers.v1": "/api/v2/registry/providers/"
}
```

This enables `terraform init` to discover private module and provider sources when the Terrapod hostname is used in `source` attributes.

---

## Complete Example: Air-Gapped Environment

For fully air-gapped environments where runner Jobs have no internet access:

1. **Pre-warm the binary cache** with the required terraform/tofu versions
2. **Pre-warm the provider cache** by running `terraform init` once from a machine with internet access (the first request populates the cache)
3. **Upload private modules** to the module registry

```yaml
api:
  config:
    registry:
      enabled: true
      provider_cache:
        enabled: true
        warm_on_first_request: true
      binary_cache:
        enabled: true
```

Runner Jobs only need network access to the Terrapod API (internal cluster networking).
