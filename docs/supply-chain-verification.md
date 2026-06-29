# Supply-chain verification

Terrapod fetches third-party artifacts from upstream — terraform/tofu/terragrunt
CLI binaries (binary cache) and provider archives (provider network mirror).
Terrapod verifies what it fetched against the publisher's signature before
trusting it, at two layers:

1. **Cache-side** — the API verifies on fetch, before anything is cached or served.
2. **Runner-side** — the runner re-verifies the **executable** against a pinned
   key immediately before it runs it, and prints the result in the run log.

Both are **on by default and fail closed**: a checksum mismatch or bad signature
is rejected, never cached, never executed.

This page covers two directions:

1. **Verifying Terrapod's own release artifacts** (below) — the images + Helm
   chart Terrapod publishes are signed and carry SBOM + provenance attestations.
2. **Terrapod verifying the upstream artifacts it fetches** ([further down](#what-is-verified-and-why-it-matters)).

## Verifying Terrapod's own release artifacts

Every released container image and the Helm chart are **keyless-signed with
[cosign](https://docs.sigstore.dev/)** (Sigstore), and each image carries an
**SBOM attestation** (SPDX) and a **SLSA build-provenance attestation** — all
anchored to the release workflow's GitHub OIDC identity and logged in the public
Rekor transparency log. No long-lived signing key exists to leak. (SPDX SBOMs
are also attached to each GitHub Release as files.)

The signing identity to pin in your verification:

- **Issuer**: `https://token.actions.githubusercontent.com`
- **Identity** (regex): `^https://github.com/mattrobinsonsre/terrapod/.github/workflows/ci.yml@refs/tags/v.*$`

### Verify an image signature

```sh
IMAGE=ghcr.io/mattrobinsonsre/terrapod-api:v0.49.0
cosign verify "$IMAGE" \
  --certificate-identity-regexp '^https://github.com/mattrobinsonsre/terrapod/.github/workflows/ci.yml@refs/tags/v.*$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Repeat for `terrapod-web`, `terrapod-runner`, `terrapod-listener`,
`terrapod-migrations`, and the chart `ghcr.io/mattrobinsonsre/terrapod:<version>`
(chart version is the tag without the leading `v`).

### Verify the SBOM attestation

The SBOM (SPDX) is attached to each image as an OCI 1.1 referrer:

```sh
gh attestation verify oci://"$IMAGE" --repo mattrobinsonsre/terrapod \
  --predicate-type https://spdx.dev/Document
```

…or with cosign (reads the same referrer):

```sh
cosign verify-attestation "$IMAGE" --type spdxjson \
  --certificate-identity-regexp '^https://github.com/mattrobinsonsre/terrapod/.github/workflows/ci.yml@refs/tags/v.*$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  | jq -r '.payload | @base64d | fromjson | .predicate' > sbom.spdx.json
```

(The SPDX SBOMs are also attached to each GitHub Release as files, if you'd
rather download them directly.)

### Verify build provenance (SLSA)

```sh
gh attestation verify oci://"$IMAGE" --repo mattrobinsonsre/terrapod \
  --predicate-type https://slsa.dev/provenance/v1
```

A deployment can enforce these at admission time (e.g. a Kyverno / Sigstore
policy-controller rule requiring a valid cosign signature for
`ghcr.io/mattrobinsonsre/terrapod-*`), so only signed Terrapod images ever run.

## What is verified, and why it matters

| Artifact | Downstream verifier? | What Terrapod's verification adds |
|---|---|---|
| **CLI binary** (terraform/tofu/terragrunt) | **None** — the binary is executed directly; nothing else checks it | The **primary** publisher-authenticity check on the executed binary |
| **Provider archive** | `terraform/tofu init` verifies against `.terraform.lock.hcl` | **Defense-in-depth** — and it stops a tampered archive being laundered into a trusted lock entry via the mirror's `h1` |

This is honest framing: for providers it is defense-in-depth (a committed lock
file already protects you); for the **executables it is the only check**, because
nothing downstream verifies the CLI binary the runner executes.

## How it works

### Cache-side (API)

On a cache miss the API fetches the publisher `SHA256SUMS` manifest and its
detached GPG signature, verifies the signature against a **pinned** publisher key
shipped in the image, checks the artifact's SHA-256 against the signed manifest,
and only then caches + serves it. The signed manifest is persisted alongside the
binary so the runner can re-verify without reaching upstream.

Pinned keys: HashiCorp (`34365D9472D7468F`), OpenTofu (`0C0AF313E5FD9F80`),
Gruntwork (`577774ACA847CC49`). Provider archives are verified against the
registry-advertised shasum and the registry's own signing key (mirroring what
`terraform init` trusts).

### Runner-side (executables)

Before executing the CLI binary, the runner re-verifies it against the signed
`SHA256SUMS` with its own pinned key and logs a visible trust line:

```
✓ verified terraform 1.9.8 (linux/amd64) — SHA-256 matches signed manifest;
  signature valid (pinned key 34365D9472D7468F, via Terrapod cache)
```

The verification material **always comes from the same source as the binary** —
the Terrapod cache when the binary came from the cache, upstream when the runner
fell back to upstream. Terrapod is never a trust anchor for authenticity: the
signature is always checked against the pinned key baked into the runner image,
so even a compromised cache cannot forge a valid publisher signature for a
tampered binary.

## Configuration

Both layers share a three-level knob (default `signature`):

| Value | Behaviour |
|---|---|
| `signature` (default) | Verify the GPG signature on `SHA256SUMS` against the pinned/advertised key **and** the artifact checksum. Fail closed. |
| `checksum` | Verify the artifact checksum against the manifest only (no signature). |
| `off` | No verification (NOT recommended). |

```yaml
# helm/terrapod/values.yaml
api:
  config:
    registry:
      binary_cache:
        verify: signature      # also drives the runner via TP_VERIFY_BINARIES
        signing_keys: {}       # operator key override (see "Key rotation" below)
      provider_cache:
        verify: signature
        allow_unsigned: false  # see "Unsigned / obscure providers" below
```

The runner inherits `binary_cache.verify` automatically (the listener injects it
into the Job), so one knob controls both API and runner.

### Unsigned / obscure providers

Provider signature verification uses the **registry-advertised** signing key, so
it works for any provider the registry signs — which the public registries
(`registry.terraform.io`, `registry.opentofu.org`) do for essentially all
providers, mainstream or obscure. But a **private/self-hosted registry or a
non-signing network mirror** may advertise no signature material. By default
(`allow_unsigned: false`) such a provider is **rejected** in `signature` mode.

Set `provider_cache.allow_unsigned: true` to **degrade to a shasum-only check
(with a warning)** for those upstreams instead of rejecting them — the archive
is still verified against the registry-advertised shasum; only the GPG-signature
step is skipped when no signature exists. It's opt-in so the secure default
stays fail-closed.

### Key rotation / operator-supplied keys

The publisher keys are **pinned** (image-baked) by default. If a publisher
rotates or expires its signing key, the pinned key stops verifying *new*
releases until an updated Terrapod image ships — a hard, fail-closed break.

To bridge a rotation without waiting for a release (or to trust an **internal
re-signing mirror**), supply the key via `binary_cache.signing_keys`, keyed by
tool. Operator-supplied keys take precedence over the bundled ones and are
propagated to runner Jobs so runner-side verification uses the same trust set:

```yaml
binary_cache:
  signing_keys:
    terraform: |
      -----BEGIN PGP PUBLIC KEY BLOCK-----
      ...your trusted key...
      -----END PGP PUBLIC KEY BLOCK-----
```

As a stopgap you can also drop `binary_cache.verify` to `checksum` (manifest
compare only) until the trust set is updated.

## Air-gapped deployments

In a sealed/air-gapped install the runner never reaches upstream: it fetches the
binary **and** the signed manifest from Terrapod, and verifies both with its
pinned key. Verification therefore holds end-to-end without any upstream
connectivity. See [Sealed (cache-only) mode](registry.md#sealed-cache-only-mode) and [Cache pre-population](registry.md#cache-pre-population) for the full air-gapped flow (point at an internal mirror → warm → seal).

## Security posture summary

For an adopter's security review, in one place:

- **Signed releases** — every image + the Helm chart is keyless cosign-signed; each image carries an SBOM (SPDX) and SLSA build-provenance attestation, all logged in Rekor ([verify](#verifying-terrapods-own-release-artifacts)).
- **Verified dependencies** — terraform/tofu/terragrunt binaries and provider archives are verified against the publisher's signed `SHA256SUMS` before being cached, served, or executed; **fail-closed by default**.
- **CI security scanning** — every change runs three layers via `scripts/pentest.sh`: **Semgrep** SAST (custom + `p/python`/`p/owasp-top-ten`/`p/secrets` rulesets), **Trivy** container-image CVE scanning, and **Nuclei** DAST against a live stack; plus CodeQL and dependency/secret scanning in `ci.yml`.
- **Hardened by default** — all pods run non-root with read-only root filesystems, dropped capabilities, and seccomp (api/web/listener/runner/jobs).
- **Reporting** — see [SECURITY.md](../SECURITY.md) for the vulnerability-disclosure policy.
