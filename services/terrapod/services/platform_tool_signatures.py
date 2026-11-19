"""Verifying the publisher's signature on a platform tool (#2589).

`platform_tools.verify` offered `off` and `checksum`, and its own description
said none of these publishers signs anything. That was **false for three of the
four**, measured against their live releases:

| tool | signing material | tlog entry |
|---|---|---|
| trivy | `<name>_<ver>_checksums.txt.sigstore.json` | `hashedrekord 0.0.1` |
| syft | the same shape | `hashedrekord 0.0.1` |
| checkov | **our own** build attestation, via the GitHub attestations API | `dsse 0.0.1` |
| opa | nothing -- only `.sha256` files | -- |

Checkov is the interesting one and the reason this is worth doing at all: its
binaries come from **Terrapod's own release**, because upstream stopped
publishing them, and `checkov-build.yml` has been attesting build provenance all
along. Nothing verified it. So the signature was already there to check -- the
gap was on the reading side, not the publishing side.

**OPA degrades to its checksum, and that is not a weaker setting.** Upstream
publishes no signature, so there is nothing in existence to verify; refusing it
would make a tool uninstallable to no benefit. The outcome is RECORDED per tool
rather than smoothed over, so `signature` never silently means `checksum`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog

logger = structlog.get_logger(__name__)

#: Every one of these is signed by a GitHub Actions workflow, so the issuer is
#: the same for all of them. An identity without an issuer is forgeable -- see
#: `ExpectedSigner` -- which is why this is never defaulted or inferred.
GITHUB_OIDC = "https://token.actions.githubusercontent.com"


@dataclass(frozen=True)
class SignerSpec:
    """Who must have signed a tool, and where its signature lives."""

    #: May carry `{version}`. **trivy's identity contains its release tag**, so
    #: a single constant identity would fail on every release but one. syft and
    #: checkov sign from `refs/heads/main`, so theirs are fixed. Measured from
    #: the certificates themselves rather than written from their docs.
    identity: str
    issuer: str
    #: `checksums` -- a `hashedrekord` bundle published beside the release,
    #: signing the checksums file whose entry covers the asset.
    #: `attestation` -- a `dsse` build-provenance attestation retrieved from the
    #: GitHub attestations API, whose subject IS the asset.
    material: Literal["checksums", "attestation"]


SIGNERS: dict[str, SignerSpec] = {
    "trivy": SignerSpec(
        identity=(
            "https://github.com/aquasecurity/trivy/.github/workflows/"
            "reusable-release.yaml@refs/tags/v{version}"
        ),
        issuer=GITHUB_OIDC,
        material="checksums",
    ),
    "syft": SignerSpec(
        identity="https://github.com/anchore/syft/.github/workflows/release.yaml@refs/heads/main",
        issuer=GITHUB_OIDC,
        material="checksums",
    ),
    "checkov": SignerSpec(
        identity=(
            "https://github.com/mattrobinsonsre/terrapod/.github/workflows/"
            "checkov-build.yml@refs/heads/main"
        ),
        issuer=GITHUB_OIDC,
        material="attestation",
    ),
    # opa is deliberately ABSENT rather than present-and-disabled: there is no
    # signature to verify, so there is no spec to write. `signature_capability`
    # reports that as `none-published`, which is a different and honest fact
    # from "we chose not to check".
}

#: What a verification attempt concluded, recorded on the cache row so
#: `signature` never silently means `checksum`.
VERIFIED = "signature-verified"
NONE_PUBLISHED = "no-signature-published"
UNAVAILABLE = "signature-unavailable"


def signature_capability(tool: str) -> str:
    """Whether `tool` can be signature-verified at all, before any network.

    Lets an operator see which tools a `signature` setting actually covers,
    rather than discovering it per-download.
    """
    return "signature" if tool in SIGNERS else NONE_PUBLISHED


def expected_signer(tool: str, version: str):
    """The `ExpectedSigner` for a tool at a version, or None if it signs nothing."""
    from terrapod.services.sigstore_verification import ExpectedSigner

    spec = SIGNERS.get(tool)
    if spec is None:
        return None
    return ExpectedSigner(identity=spec.identity.format(version=version), issuer=spec.issuer)


#: Where a build attestation for one of OUR OWN published assets lives.
#: **Anonymous -- measured.** A deployment needs no GitHub credential to verify
#: checkov, which is what makes this usable rather than a maintainer-only check.
ATTESTATION_API = "https://api.github.com/repos/{repo}/attestations/sha256:{digest}"

#: The repository whose CI builds and publishes checkov. It is ours, which is
#: the whole reason this tool can be signature-verified at all: upstream stopped
#: publishing binaries, so Terrapod builds them -- and has been attesting them
#: since #2066, with nothing on the reading side to check it.
CHECKOV_REPO = "mattrobinsonsre/terrapod"


async def verify_signature(
    client,
    *,
    tool: str,
    version: str,
    asset: str,
    base: str,
    asset_digest: str,
) -> str:
    """Verify the publisher's signature over `asset`, returning what happened.

    Returns `VERIFIED`, or `NONE_PUBLISHED` for a tool whose publisher signs
    nothing. Raises `SigstoreVerificationError` when a signature exists and
    genuinely fails -- that is fail-closed, and the caller must not cache the
    artifact.

    **Runs after the checksum, not instead of it.** The checksum establishes
    which bytes arrived; this establishes who vouched for them. For the
    `checksums` tools the signature is over the MANIFEST, so the chain is:
    the manifest is signed by the publisher, and the asset's digest is a line in
    that manifest -- which the existing checksum gate already proves.
    """
    from terrapod.http_retry import arequest_with_retry
    from terrapod.services.sigstore_verification import verify_attestation, verify_digest

    spec = SIGNERS.get(tool)
    if spec is None:
        return NONE_PUBLISHED

    expect = expected_signer(tool, version)

    if spec.material == "checksums":
        import hashlib

        manifest_url = f"{base}/v{version}/{tool}_{version}_checksums.txt"
        bundle_url = f"{manifest_url}.sigstore.json"

        manifest = await arequest_with_retry(client, "GET", manifest_url)
        bundle = await arequest_with_retry(client, "GET", bundle_url)
        if manifest.status_code != 200 or bundle.status_code != 200:
            raise _unavailable(
                f"could not fetch {tool} signing material for {version} "
                f"(manifest HTTP {manifest.status_code}, bundle HTTP {bundle.status_code})"
            )

        # The signature is over the manifest's BYTES, so hash exactly what was
        # served -- not a re-serialisation, and not the text after any decoding
        # round-trip.
        manifest_digest = hashlib.sha256(manifest.content).hexdigest()
        await verify_digest(digest_hex=manifest_digest, bundle_json=bundle.content, expect=expect)
        logger.info(
            "Platform tool signature verified",
            tool=tool,
            version=version,
            asset=asset,
            signer=expect.identity,
        )
        return VERIFIED

    # `attestation`: the statement's subject IS the asset, so there is no
    # manifest hop -- the attestation is checked directly against the digest the
    # checksum gate established.
    url = ATTESTATION_API.format(repo=CHECKOV_REPO, digest=asset_digest)
    resp = await arequest_with_retry(client, "GET", url)
    if resp.status_code != 200:
        raise _unavailable(
            f"could not fetch the {tool} build attestation for {asset} (HTTP {resp.status_code})"
        )
    attestations = (resp.json() or {}).get("attestations") or []
    if not attestations:
        raise _unavailable(f"no build attestation is published for {tool} {asset}")

    import json as _json

    bundle_json = _json.dumps(attestations[0]["bundle"]).encode()
    predicate, _statement = await verify_attestation(
        digest_hex=asset_digest, bundle_json=bundle_json, expect=expect
    )
    logger.info(
        "Platform tool attestation verified",
        tool=tool,
        version=version,
        asset=asset,
        predicate=predicate,
        signer=expect.identity,
    )
    return VERIFIED


def _unavailable(message: str):
    """A transport failure, which says nothing about the artifact.

    Distinct from a verification failure on purpose: the caller may degrade on
    this and must never degrade on a signature that actually failed.
    """
    from terrapod.services.sigstore_verification import TrustRootUnavailable

    return TrustRootUnavailable(message)
