# Publishing to the Private Registry with `terrapod-publish`

Terrapod ships a CLI, `terrapod-publish`, that publishes providers and
modules to a self-hosted Terrapod private registry. It is the operator-
facing companion to the registry: build your provider binaries (or point
it at a module directory), and `terrapod-publish` packages, signs, and
uploads everything.

This page is the operator runbook for publishing. For the registry's
read side (`terraform init` consumption, caching, RBAC, VCS-driven
publishing) see [Registry](registry.md).

> This page covers **direct, client-signed publishing**. If your modules
> live in git and you'd rather have Terrapod watch the tag stream and
> publish versions automatically, see
> [VCS-Driven Module Publishing](registry.md#vcs-driven-module-publishing).

---

## The client-signed model

Provider publishing in Terrapod is **client-signed, direct, and
streamed**. The publisher — not the server — owns the GPG signature over
the provider's `SHA256SUMS` manifest.

1. `terrapod-publish` zips each provider platform binary, computes
   `SHA256SUMS` over the zips, and **GPG-signs `SHA256SUMS` with the
   publisher's own private key** (pure-Go OpenPGP — no `gpg` binary on
   the publishing host).
2. The manifest, then the detached signature, then each platform zip are
   streamed directly to the API in a fixed order. There are no presigned
   URLs and no separate "finalize" step.
3. The server verifies the signature against a **registered** GPG public
   key before it will accept any binary. The signature is the trust
   gate: a binary whose SHA doesn't appear in the signed manifest, or a
   signature from an unregistered key, is rejected with `422`.
4. The server **never re-signs**. When `terraform init` downloads the
   provider, the download response advertises the publisher's own public
   key in `signing_keys.gpg_public_keys`. The chain of custody runs
   publisher → registry → consumer with one signature throughout.

Because the server trusts the signature rather than the upload identity,
the publisher's GPG **public** key must be registered with Terrapod
before the first publish. Registration is the one-time setup step below.

Modules are simpler — there is no signature. A module publish is a single
streamed upload of the gzipped source tarball. The server extracts the
module interface (inputs/outputs) and triggers impact runs on any linked
workspaces. See [Module Impact Analysis](registry.md#module-impact-analysis).

---

## Prerequisites

Before the first publish you need three things in place: a registered GPG
public key, a provider (or module) slot, and an API token. The first two
are best managed declaratively with the `terrapod` Terraform provider in
a small `terrapod-config` repo, so they live in version control and can
be reviewed — but the equivalent API calls are shown for completeness.

### 1. Register the publisher's GPG public key

The server verifies provider signatures against keys you register ahead
of time. Register the **public** half of the key `terrapod-publish` will
sign with.

Declaratively (recommended), in your `terrapod-config`:

```hcl
resource "terrapod_gpg_key" "publisher" {
  ascii_armor = file("${path.module}/publisher-public-key.asc")
}
```

Or directly against the API:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  https://terrapod.example.internal/api/terrapod/v1/gpg-keys \
  -d @- <<'JSON'
{
  "data": {
    "type": "gpg-keys",
    "attributes": {
      "ascii-armor": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END PGP PUBLIC KEY BLOCK-----\n"
    }
  }
}
JSON
```

The `key_id` is extracted from the armor at creation time. Only the
public key is registered — the private key never leaves the publishing
host.

### 2. Create the provider (or module) slot

The registry entry must exist before a version can be uploaded. The
version itself is created **implicitly on first upload** — there is no
separate version-create step.

Provider, declaratively:

```hcl
resource "terrapod_registry_provider" "example" {
  name = "example"
}
```

Module, declaratively:

```hcl
resource "terrapod_registry_module" "vpc" {
  name     = "vpc"
  provider = "aws"
}
```

Or via the API:

```bash
# Provider slot
curl -sS -X POST \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  https://terrapod.example.internal/api/terrapod/v1/registry-providers \
  -d '{"data":{"type":"registry-providers","attributes":{"name":"example"}}}'

# Module slot
curl -sS -X POST \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  https://terrapod.example.internal/api/terrapod/v1/registry-modules \
  -d '{"data":{"type":"registry-modules","attributes":{"name":"vpc","provider":"aws"}}}'
```

Creating a slot requires any authenticated user; the creator becomes the
owner (registry `admin`). Publishing versions requires `write` on the
slot. See [Registry RBAC](registry.md#rbac).

### 3. An API token

`terrapod-publish` authenticates with a Terrapod API token. The simplest
way to get one is `terraform login terrapod.example.internal`, which
stores the token in `~/.terraform.d/credentials.tfrc.json` — the CLI
reads it from there automatically. For CI, create a long-lived token
(`settings → tokens` in the UI, or `POST /api/terrapod/v1/account/tokens`)
and pass it via `$TERRAPOD_TOKEN`. See [Auth resolution](#auth-resolution).

---

## Installing `terrapod-publish`

`terrapod-publish` is a Go binary published as a GitHub Release artifact
on every Terrapod release tag, alongside `terrapod-migrate` and the
Terraform provider. Each release publishes:

- **macOS universal** — a single fat binary that runs natively on Intel
  and Apple Silicon.
- **Linux** — amd64 + arm64.
- **Windows** — amd64 + arm64.

Each archive ships with a SHA256 checksum file, detached-GPG-signed with
the project signing key.

Download the artifact for your platform from the
[Releases page](https://github.com/mattrobinsonsre/terrapod/releases),
verify the checksum, and place the binary on your `PATH`:

```bash
tar -xzf terrapod-publish_linux_amd64.tar.gz
sudo install terrapod-publish /usr/local/bin/
terrapod-publish --version
```

---

## Publishing a provider

```
terrapod-publish provider \
  --host HOST \
  --name NAME \
  --version VERSION \
  --signing-key KEY.asc \
  --binary OS/ARCH=PATH [--binary OS/ARCH=PATH ...]
```

The CLI does the packaging, hashing, and signing entirely in Go. You
supply only the **pre-built provider binaries** — one per platform.
Cross-compilation (`go build` with `GOOS`/`GOARCH`) stays in your build
pipeline; `terrapod-publish` never invokes the Go toolchain.

| Flag | Meaning |
|---|---|
| `--host` | Terrapod hostname (e.g. `terrapod.example.internal`). |
| `--name` | Provider name — the slot you created (e.g. `example`). |
| `--version` | Semantic version, no `v` prefix (e.g. `1.4.0`). |
| `--signing-key` | Path to the **private** signing key in ASCII-armored form. Its public half must already be registered (prerequisite 1). |
| `--binary OS/ARCH=PATH` | A built binary for one platform. Repeatable. The CLI zips each, computes its SHA, and includes it in the signed manifest. |
| `--signing-key-passphrase` | Passphrase for the signing key. Omit for a passphrase-less key. Also `$TERRAPOD_SIGNING_KEY_PASSPHRASE`. |
| `--token` | API token. See [Auth resolution](#auth-resolution). |

### What the CLI does, in order

For a provider with N platforms, `terrapod-publish` performs these uploads
against the API in this exact order:

1. `PUT .../versions/{version}/shasums` — the `SHA256SUMS` manifest.
2. `PUT .../versions/{version}/shasums.sig` — the detached signature.
   The server verifies it against your registered key over the manifest.
   **This is the trust gate** — a bad or unregistered signature fails here
   with `422`, and no binaries are accepted.
3. `PUT .../versions/{version}/platforms/{os}/{arch}` (× N) — each zip is
   streamed to disk and its SHA checked against the signed manifest;
   `422` on mismatch.

The version is created implicitly when the manifest lands; there is no
explicit create or finalize call.

### Worked example: publish six platforms

Build the six standard platforms in your pipeline. A provider's binary is
just the compiled plugin executable — name it
`terraform-provider-{name}_v{version}`:

```bash
VERSION=1.4.0
NAME=example
OUT=dist

for platform in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64; do
  os="${platform%/*}"; arch="${platform#*/}"
  ext=""; [ "$os" = "windows" ] && ext=".exe"
  GOOS="$os" GOARCH="$arch" go build \
    -o "$OUT/$os/$arch/terraform-provider-${NAME}_v${VERSION}${ext}" \
    ./...
done
```

Then publish all six in one invocation:

```bash
terrapod-publish provider \
  --host terrapod.example.internal \
  --name "$NAME" \
  --version "$VERSION" \
  --signing-key ./publisher-private-key.asc \
  --binary linux/amd64=dist/linux/amd64/terraform-provider-example_v1.4.0 \
  --binary linux/arm64=dist/linux/arm64/terraform-provider-example_v1.4.0 \
  --binary darwin/amd64=dist/darwin/amd64/terraform-provider-example_v1.4.0 \
  --binary darwin/arm64=dist/darwin/arm64/terraform-provider-example_v1.4.0 \
  --binary windows/amd64=dist/windows/amd64/terraform-provider-example_v1.4.0.exe \
  --binary windows/arm64=dist/windows/arm64/terraform-provider-example_v1.4.0.exe
```

After a successful publish, consumers can pull the provider:

```hcl
terraform {
  required_providers {
    example = {
      source  = "terrapod.example.internal/default/example"
      version = "1.4.0"
    }
  }
}
```

`terraform init` fetches the platform zip and the publisher's public key
(advertised in `signing_keys.gpg_public_keys`) and verifies the signature
itself — exactly as it would against the public registry.

---

## Publishing a module

```
terrapod-publish module \
  --host HOST \
  --name NAME \
  --provider PROVIDER \
  --version VERSION \
  --source ./moduledir
```

Modules are unsigned. The CLI gzips the contents of `--source` into a
tarball and streams it in a single `PUT` to the module upload endpoint.
The server extracts the module interface (inputs and outputs) and triggers
impact runs on any linked workspaces.

| Flag | Meaning |
|---|---|
| `--host` | Terrapod hostname. |
| `--name` | Module name — the slot you created (e.g. `vpc`). |
| `--provider` | The module's provider segment (e.g. `aws`). |
| `--version` | Semantic version, no `v` prefix. |
| `--source` | Path to the module directory to package. |
| `--token` | API token. See [Auth resolution](#auth-resolution). |

```bash
terrapod-publish module \
  --host terrapod.example.internal \
  --name vpc \
  --provider aws \
  --version 2.1.0 \
  --source ./modules/vpc
```

Consume it the usual way:

```hcl
module "vpc" {
  source  = "terrapod.example.internal/default/vpc/aws"
  version = "2.1.0"
}
```

---

## Auth resolution

`terrapod-publish` resolves the API token from the first source that
yields a value, in this order:

1. The `--token` flag.
2. The `$TERRAPOD_TOKEN` environment variable.
3. `~/.terraform.d/credentials.tfrc.json` — the token stored by
   `terraform login HOST` for the matching host.

For interactive use, `terraform login terrapod.example.internal` once and
the CLI picks the token up automatically. For CI, prefer `$TERRAPOD_TOKEN`
sourced from a secret store.

The signing-key passphrase resolves from `--signing-key-passphrase`, then
`$TERRAPOD_SIGNING_KEY_PASSPHRASE`. Omit both for a passphrase-less key.

---

## CI example (GitHub Actions)

Build the provider binaries per platform, then publish on a version tag.
Cross-compilation is plain `go build`; `terrapod-publish` handles the rest.

```yaml
# .github/workflows/publish-provider.yml
name: publish-provider
on:
  push:
    tags: ["v*"]

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-go@v5
        with:
          go-version: "1.23"

      - name: Build provider binaries
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          NAME=example
          for platform in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64; do
            os="${platform%/*}"; arch="${platform#*/}"
            ext=""; [ "$os" = "windows" ] && ext=".exe"
            GOOS="$os" GOARCH="$arch" go build \
              -o "dist/$os/$arch/terraform-provider-${NAME}_v${VERSION}${ext}" ./...
          done

      - name: Install terrapod-publish
        run: |
          curl -sSL -o tp.tar.gz \
            https://github.com/mattrobinsonsre/terrapod/releases/latest/download/terrapod-publish_linux_amd64.tar.gz
          tar -xzf tp.tar.gz
          sudo install terrapod-publish /usr/local/bin/

      - name: Publish provider
        env:
          TERRAPOD_TOKEN: ${{ secrets.TERRAPOD_TOKEN }}
          TERRAPOD_SIGNING_KEY_PASSPHRASE: ${{ secrets.SIGNING_KEY_PASSPHRASE }}
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          printf '%s' "${{ secrets.SIGNING_PRIVATE_KEY }}" > signing-key.asc
          terrapod-publish provider \
            --host terrapod.example.internal \
            --name example \
            --version "$VERSION" \
            --signing-key signing-key.asc \
            --binary linux/amd64=dist/linux/amd64/terraform-provider-example_v${VERSION} \
            --binary linux/arm64=dist/linux/arm64/terraform-provider-example_v${VERSION} \
            --binary darwin/amd64=dist/darwin/amd64/terraform-provider-example_v${VERSION} \
            --binary darwin/arm64=dist/darwin/arm64/terraform-provider-example_v${VERSION} \
            --binary windows/amd64=dist/windows/amd64/terraform-provider-example_v${VERSION}.exe \
            --binary windows/arm64=dist/windows/arm64/terraform-provider-example_v${VERSION}.exe
          rm -f signing-key.asc
```

The token and signing material come from repository/organization secrets.
The signing **public** key was registered with Terrapod once, ahead of
time (prerequisite 1); CI only ever holds the private key transiently.

For a module the CI step collapses to a single `terrapod-publish module`
call with `--source` pointed at the module directory — no build step, no
signing key.

---

## Troubleshooting

### `422 Unprocessable Entity`

The trust gate or a content check rejected the upload. The three common
causes:

- **Unregistered signing key.** The signature verified cryptographically
  but the key isn't registered with Terrapod, or the wrong key was used.
  Register the **public** half of your signing key
  (`POST /api/terrapod/v1/gpg-keys` or the `terrapod_gpg_key` resource)
  and confirm the `key_id` matches the key behind `--signing-key`.
- **Signature doesn't verify.** The detached signature doesn't validate
  against the manifest. This usually means the manifest and signature came
  from different runs, or the signing key doesn't match the registered
  public key. Re-run the publish so the manifest and signature are
  produced together.
- **SHA mismatch on a platform.** A platform zip's SHA doesn't match the
  entry in the signed manifest — typically a binary changed between
  signing and upload, or the wrong file was passed to `--binary`. Re-run
  the full publish so the manifest covers exactly the binaries uploaded.

Binaries are refused until the signature is verified, and they must be
uploaded **after** the manifest and signature. The CLI always uploads in
the correct order (manifest → signature → platforms); a `422` on a
platform upload from a hand-rolled client almost always means the order
was wrong or the signature step was skipped.

### `401 Unauthorized`

No usable token was found, or the token is invalid/expired. Check the
[auth resolution](#auth-resolution) order: pass `--token`, set
`$TERRAPOD_TOKEN`, or run `terraform login HOST`. If the token was issued
a long time ago, it may have hit the configured API-token max TTL — mint a
fresh one.

### `403 Forbidden`

The token is valid but lacks permission. Publishing a version requires
`write` on the registry slot (the creator/owner has `admin`). Check the
slot's `permissions` block in its show/list response, or have an admin
grant you access. See [Registry RBAC](registry.md#rbac).

### `404 Not Found`

The provider or module slot doesn't exist. Create it first (prerequisite
2) — the version is created implicitly on upload, but the slot is not.

---

## See also

- [Registry](registry.md) — consuming providers/modules, caching, RBAC,
  VCS-driven module publishing.
- [API Reference — Registry](api-reference.md#registry----providers) —
  the raw publish endpoints.
- [Migration](migration.md) — `terrapod-migrate`, for bulk-importing an
  existing TFE/HCP private registry.
