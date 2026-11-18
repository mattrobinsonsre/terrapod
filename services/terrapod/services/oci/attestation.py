"""Finding and verifying the Sigstore attestations an OCI upstream publishes (#2589).

`sigstore_verification` knows how to verify a bundle. This knows where an OCI
registry keeps one, which turned out to be the part worth writing down: the
layout is not what cosign's own tooling reports.

**Measured against ghcr.io, because reading did not get there.** For an image at
``sha256:<hex>``:

- the **referrers API** (`GET /v2/<repo>/referrers/<digest>`), which is the
  OCI 1.1 answer and the one to prefer, returns **404** on ghcr today;
- ``cosign triangulate`` names ``<repo>:sha256-<hex>.sig``, and that tag **does
  not exist** -- a direct fetch is 404 with a control proving the token and
  headers were fine. It is the deprecated command describing the legacy layout;
- what actually holds the bundles is the **bare** ``<repo>:sha256-<hex>`` tag,
  an OCI image index. 199 such tags exist in one of our own repositories and
  not one ends in ``.sig``.

Each entry in that index carries ``artifactType:
application/vnd.dev.sigstore.bundle.v0.3+json`` and annotations naming what it
attests:

    dev.sigstore.bundle.content:       dsse-envelope
    dev.sigstore.bundle.predicateType: https://slsa.dev/provenance/v1

Those annotations are why this module can find the one attestation a caller
wants without downloading the rest -- which matters, because an SPDX SBOM
attestation runs to megabytes while the manifest entry naming it is under a
kilobyte.

So discovery tries, in order: the referrers API, then the bare tag, then the
legacy ``.sig`` tag. All three are attempted rather than only the one that works
on ghcr, because the first is where the ecosystem is going and the third is
where it has been, and a deployment mirrors whatever its upstream actually does.

**An absent attestation is not a failure.** Most OCI content in the world is
unsigned, and refusing it would make this a switch nobody could turn on. The
honest outcome is that the artifact's attestor is recorded as none-published,
which is exactly the distinction the ledger exists to carry. A *present*
attestation that does not verify is a different matter and fails closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import structlog

from terrapod.services.oci import pullthrough_service as pull

logger = structlog.get_logger(__name__)

#: The artifactType a Sigstore bundle carries inside an OCI index.
BUNDLE_ARTIFACT_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"

#: The annotation naming what a bundle attests, so a caller can pick one
#: without fetching every bundle in the index.
PREDICATE_ANNOTATION = "dev.sigstore.bundle.predicateType"

#: Accept header wide enough for an index or a single manifest. The bundle tag
#: is an index on ghcr; a registry is free to serve a bare manifest instead.
_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


@dataclass(frozen=True, slots=True)
class DiscoveredBundle:
    """A bundle this module found, and what the registry says it is about.

    `predicate` comes from the index annotation rather than from the bundle, so
    it is a *claim by the registry* and not yet verified. It is useful for
    choosing which bundle to fetch and must never be trusted as the answer --
    the verified predicate comes back from `verify_attestation`, which reads it
    out of the signed payload.
    """

    digest: str
    size: int
    predicate: str


def bundle_tag(digest: str) -> str:
    """The tag a bundle index sits at for `digest`.

    `sha256:<hex>` becomes `sha256-<hex>`: a tag cannot contain a colon, which
    is the whole reason for the substitution rather than it being a convention
    worth remembering.
    """
    return digest.replace(":", "-", 1)


def _candidates_in(document: dict) -> list[DiscoveredBundle]:
    """Every index entry that might be a bundle, with what the index claims.

    **The index entry's `artifactType` cannot be trusted to identify a bundle,
    and this is the one thing in this module that had to be measured rather
    than read.** In our own registry the primary cosign signature appears in
    the index as

        artifactType: application/vnd.oci.empty.v1+json

    -- which is its *config* media type -- while the manifest it points at
    declares `artifactType: application/vnd.dev.sigstore.bundle.v0.3+json` and
    carries a bundle layer. So filtering the index on the bundle artifactType
    silently drops the signature and keeps only the attestations: two of three,
    and the missing one is the one that matters most.

    Hence every entry is a candidate and `_is_bundle_manifest` decides from the
    manifest. The index is tiny -- three entries of under a kilobyte here -- so
    the extra fetches cost far less than being wrong.

    The `predicate` carried back is the index's *annotation*, which is absent
    on the signature entry and present on each attestation. It is a hint for
    choosing what to fetch, never an answer: the verified predicate comes out
    of the signed payload.
    """
    found: list[DiscoveredBundle] = []
    for entry in document.get("manifests") or []:
        if not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        if not isinstance(digest, str) or not digest:
            continue
        annotations = entry.get("annotations") or {}
        found.append(
            DiscoveredBundle(
                digest=digest,
                size=int(entry.get("size") or 0),
                predicate=str(annotations.get(PREDICATE_ANNOTATION) or ""),
            )
        )
    return found


def _is_bundle_manifest(document: dict) -> bool:
    """Whether a fetched manifest is a Sigstore bundle.

    Accepts either signal, because the two disagree in the wild: the manifest's
    own `artifactType`, or any layer whose mediaType is the bundle type.
    """
    if document.get("artifactType") == BUNDLE_ARTIFACT_TYPE:
        return True
    return any(
        isinstance(layer, dict) and layer.get("mediaType") == BUNDLE_ARTIFACT_TYPE
        for layer in document.get("layers") or []
    )


async def discover_bundles(
    host: str,
    repository: str,
    digest: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[DiscoveredBundle]:
    """Find the Sigstore bundles an upstream publishes for `digest`.

    Returns an empty list when the upstream publishes none, which is the common
    case and not an error -- see the module docstring on why refusing unsigned
    content would make this unusable.

    Tries the referrers API, then the bare bundle tag, then the legacy `.sig`
    tag, and stops at the first that yields bundles.
    """
    tag = bundle_tag(digest)
    # Ordered: where the ecosystem is going, where it is, where it has been.
    # The referrers lookup is its own function rather than a reference bent
    # through `fetch_manifest`, which would build a `/manifests/` URL and ask
    # the wrong question entirely.
    attempts: tuple[tuple[str, str | None], ...] = (
        ("referrers", None),
        ("bundle-tag", tag),
        ("legacy-sig-tag", f"{tag}.sig"),
    )

    for label, reference in attempts:
        body: bytes | None
        if reference is None:
            body = await _fetch_referrers(host, repository, digest, client=client)
        else:
            try:
                body, _media, _ = await pull.fetch_manifest(
                    host, repository, reference, client=client
                )
            except pull.UpstreamUnavailable:
                continue
        if body is None:
            continue

        try:
            document = json.loads(body)
        except ValueError:
            continue
        if not isinstance(document, dict):
            continue

        bundles = _candidates_in(document)
        if bundles:
            logger.info(
                "Found Sigstore attestations for an OCI artifact",
                host=host,
                repository=repository,
                digest=digest,
                via=label,
                count=len(bundles),
                predicates=[b.predicate for b in bundles if b.predicate],
            )
            return bundles

    logger.info(
        "No Sigstore attestation published for an OCI artifact",
        host=host,
        repository=repository,
        digest=digest,
    )
    return []


async def _fetch_referrers(
    host: str,
    repository: str,
    digest: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> bytes | None:
    """The OCI 1.1 referrers index, or None when the registry does not serve it.

    Separate from `fetch_manifest` because the referrers endpoint is not under
    `/manifests/`, and a 404 here means "this registry does not implement it"
    rather than "there is nothing" -- ghcr returns 404 even where the bundles
    demonstrably exist under a tag.
    """
    from terrapod.http_retry import arequest_with_retry

    url = f"{pull._base_url(host)}/v2/{repository}/referrers/{digest}"
    auth = pull._auth(host)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True, auth=auth)
    try:
        response = await arequest_with_retry(
            client, "GET", url, headers={"Accept": _ACCEPT}, auth=auth, timeout=30.0
        )
    except httpx.HTTPError:
        return None
    finally:
        if owns:
            await client.aclose()

    if response.status_code != 200:
        return None
    return response.content


#: What the ledger records about how an artifact came to be trusted. The names
#: are the vocabulary #2589's attestation field uses, so an operator reading a
#: row and an operator reading a log see the same words.
ATTEST_UPSTREAM_PUBLISHER = "upstream-publisher"
ATTEST_NONE_PUBLISHED = "none-published"
ATTEST_NOT_CONFIGURED = "none-configured"


class AttestationRefused(Exception):
    """An artifact was refused because its attestation was absent or bad.

    Fail-closed: the caller must not store the manifest. Raised for two
    different situations deliberately kept distinguishable in the message,
    because they want different responses -- an absent attestation on an
    upstream configured to require one is a configuration decision biting, and
    a *present* attestation that does not verify is a supply-chain event.
    """


async def _bundle_bytes(
    host: str,
    repository: str,
    manifest_digest: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> bytes | None:
    """Fetch the bundle JSON a bundle manifest points at.

    Read wholly into memory rather than streamed, which is the opposite of what
    `fetch_blob` does for an image layer and is right here: a Sigstore bundle is
    kilobytes (10,743 bytes for the signature on one of our own images), the
    verifier needs the whole document at once, and the PVC-tempfile rule exists
    for things that can realistically reach tens of megabytes.
    """
    from terrapod.http_retry import arequest_with_retry

    try:
        body, _media, _ = await pull.fetch_manifest(
            host, repository, manifest_digest, client=client
        )
    except pull.UpstreamUnavailable:
        return None

    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict) or not _is_bundle_manifest(document):
        return None

    layer = next(
        (
            layer
            for layer in document.get("layers") or []
            if isinstance(layer, dict) and layer.get("mediaType") == BUNDLE_ARTIFACT_TYPE
        ),
        None,
    )
    if layer is None or not isinstance(layer.get("digest"), str):
        return None

    url = f"{pull._base_url(host)}/v2/{repository}/blobs/{layer['digest']}"
    auth = pull._auth(host)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True, auth=auth)
    try:
        response = await arequest_with_retry(
            client, "GET", url, auth=auth, follow_redirects=True, timeout=30.0
        )
    except httpx.HTTPError:
        return None
    finally:
        if owns:
            await client.aclose()

    return response.content if response.status_code == 200 else None


async def attest_before_admission(
    host: str,
    repository: str,
    digest: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Check an artifact's publisher attestation, returning what to record.

    Called between fetching a manifest and storing it, which is the placement
    that matters: nothing unverified is written, so a refusal leaves the cache
    as it was rather than needing to be undone.

    Returns one of `ATTEST_NOT_CONFIGURED`, `ATTEST_NONE_PUBLISHED` or
    `ATTEST_UPSTREAM_PUBLISHER`. Raises `AttestationRefused` when the artifact
    must not be admitted.

    The three outcomes are not a severity scale and the middle one is not a
    failure. An upstream with no expected signer is not being checked, and an
    upstream that publishes nothing cannot be -- recording which is the whole
    point, because "we did not check" and "we checked and nobody signed it" are
    different facts and a single boolean loses both.
    """
    from terrapod.config import settings
    from terrapod.services import sigstore_verification as sigver

    upstream = next(
        (u for u in settings.registry.oci.upstreams if u.host == host),
        None,
    )
    if upstream is None or not upstream.signer_identity.strip():
        return ATTEST_NOT_CONFIGURED

    expect = sigver.ExpectedSigner(
        identity=upstream.signer_identity.strip(),
        issuer=upstream.signer_issuer.strip(),
    )
    bundles = await discover_bundles(host, repository, digest, client=client)
    if not bundles:
        if upstream.require_attestation:
            raise AttestationRefused(
                f"{host}/{repository}@{digest} publishes no Sigstore attestation, and "
                f"this upstream is configured to require one from {expect.identity}"
            )
        return ATTEST_NONE_PUBLISHED

    digest_hex = digest.split(":", 1)[-1]
    verified_any = False
    last_error: str | None = None

    for candidate in bundles:
        raw = await _bundle_bytes(host, repository, candidate.digest, client=client)
        if raw is None:
            continue
        try:
            await sigver.verify_attestation(digest_hex=digest_hex, bundle_json=raw, expect=expect)
        except sigver.TrustRootUnavailable as exc:
            # Says nothing about the artifact, so it must not read as a bad
            # one. Refused rather than passed because admitting unverified
            # content on an upstream an operator asked to verify would be the
            # quiet failure this whole feature exists to remove.
            raise AttestationRefused(
                f"{host}/{repository}@{digest} could not be verified because the "
                f"Sigstore trust root was unavailable: {exc}"
            ) from exc
        except sigver.SigstoreVerificationError as exc:
            last_error = str(exc)
            continue
        verified_any = True
        break

    if not verified_any:
        # A bundle was published and none of them verified as the expected
        # signer. Fail-closed regardless of `require_attestation`: that switch
        # decides whether an ABSENT attestation is tolerated, never whether a
        # BAD one is.
        raise AttestationRefused(
            f"{host}/{repository}@{digest} publishes {len(bundles)} Sigstore "
            f"attestation(s) and none verified as {expect.identity} via "
            f"{expect.issuer}: {last_error or 'no bundle could be fetched'}"
        )

    return ATTEST_UPSTREAM_PUBLISHER
