"""Publisher provenance for the package caches (#2589, Gap A).

A digest check answers "are these the bytes the index named". A publisher check
answers "did the publisher say so", which is a different and stronger claim: an
index entry and the artifact it names can both be replaced by whoever controls
the index, and a digest verifies them against each other either way.

Two ecosystems publish something we can check, and **they differ in kind rather
than in detail**, which is why this module has two functions and not one
parameterised one:

* **PyPI** publishes PEP 740 attestations: a Sigstore-signed in-toto statement
  whose certificate names the repository and workflow that built the artifact.
  So the fact recorded is *which publisher*, and it is per package.
* **npm** signs the artifact with the **registry's own** key, published at a
  well-known endpoint. The fact recorded is *the registry vouched for this*,
  and it is the same key for every package.

What this module does NOT do is demand a configured expected signer. #2589's
settled decisions put admission policy in OPA (decision 3) and ship the content
allowlist observe-only first (decision 5), so verification's job is to establish
a fact and record it; deciding whether a given publisher is acceptable is
policy's. Demanding an operator pre-register a repository and workflow for every
transitive dependency would be unusable, and the failure mode of an unusable
control is that it gets switched off.

**A present-but-invalid signature is always fatal**, whatever the configuration.
That asymmetry is the same one the OCI path takes: configuration decides whether
an *absent* attestation is tolerated, never whether a *bad* one is.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger(__name__)

#: npm's own key endpoint. Not configurable: it is the registry's identity, and
#: an operator who could point it elsewhere could point it at their own key,
#: which would make the check decorative.
NPM_KEYS_URL = "https://registry.npmjs.org/-/npm/v1/keys"

#: PyPI's integrity endpoint. The provenance is NOT on the legacy JSON API --
#: `/pypi/<name>/json` carries no provenance field at all -- so it has its own
#: base, which follows the configured upstream so a mirror can serve it.
PYPI_PROVENANCE_PATH = "/integrity/{name}/{version}/{filename}/provenance"

_IN_TOTO = "application/vnd.in-toto+json"


class ProvenanceError(Exception):
    """A publisher's signature or attestation was present and did not verify.

    Always fatal. Distinct from "nothing was published", which is an ordinary
    outcome recorded as such.
    """


class ProvenanceUnavailable(Exception):
    """The check could not be performed, which is not the same as failing it.

    An unreachable trust root or a registry that answered 5xx says nothing about
    the artifact. The caller decides: today it degrades to the digest check and
    records that, rather than refusing an artifact on the strength of our own
    egress.
    """


@dataclass(frozen=True, slots=True)
class Provenance:
    """What a verified publisher check established.

    `identity` is empty for npm, deliberately: there is no per-package publisher
    identity to record when the registry's own key is what signed it, and
    inventing one ("npm") would read as a publisher claim nobody made.
    """

    #: Who signed, where there is such a thing. A Fulcio SAN for PyPI.
    identity: str
    #: Which OIDC issuer vouched for that identity, where applicable.
    issuer: str
    #: What kind of statement it was, for the ledger to record verbatim.
    predicate: str
    #: Free-text description of the mechanism, for an operator reading a row.
    mechanism: str


def _canonical_npm_message(name: str, version: str, integrity: str) -> bytes:
    """npm's signed message.

    `<name>@<version>:<integrity>`, where `integrity` is the SRI string from the
    version document -- not a hex digest, and not recomputed by us. Measured
    against the live registry; a tampered message is refused.
    """
    return f"{name}@{version}:{integrity}".encode()


async def _npm_keys(client: httpx.AsyncClient) -> dict[str, str]:
    """`keyid -> base64 SPKI` for every key npm publishes."""
    try:
        response = await client.get(NPM_KEYS_URL, timeout=20.0)
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProvenanceUnavailable(f"could not read npm's signing keys: {exc}") from exc
    return {
        k["keyid"]: k["key"] for k in document.get("keys", []) if k.get("keyid") and k.get("key")
    }


def _verify_npm_signature(spki_b64: str, signature_b64: str, message: bytes) -> None:
    """ECDSA P-256 over npm's canonical message.

    Imported inside the function so a deployment that never fetches from npm
    does not pay the import, matching how the rest of this package treats its
    heavier dependencies.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(spki_b64[i : i + 64] for i in range(0, len(spki_b64), 64))
        + "\n-----END PUBLIC KEY-----\n"
    )
    try:
        public_key = load_pem_public_key(pem.encode())
    except Exception as exc:  # malformed key material from the endpoint
        raise ProvenanceUnavailable(f"npm published an unreadable key: {exc}") from exc

    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ProvenanceUnavailable(f"npm key is {type(public_key).__name__}, not ECDSA")

    try:
        public_key.verify(base64.b64decode(signature_b64), message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ProvenanceError(
            "npm's registry signature does not verify against the key it names "
            "-- the artifact is not what the registry signed"
        ) from exc


async def verify_npm(
    client: httpx.AsyncClient, *, name: str, version: str, document: dict
) -> Provenance | None:
    """Verify npm's registry signature. None when the version carries none.

    `document` is npm's version document, which is already fetched on the miss
    path -- so this adds no round trip beyond the keys endpoint.
    """
    dist = document.get("dist") or {}
    signatures = dist.get("signatures") or []
    integrity = dist.get("integrity") or ""
    if not signatures or not integrity:
        return None

    keys = await _npm_keys(client)
    message = _canonical_npm_message(name, version, integrity)

    for signature in signatures:
        key_id = signature.get("keyid")
        spki = keys.get(key_id or "")
        if not spki:
            # A signature naming a key the registry does not publish is not
            # something to shrug at: it is either a rotation we have not seen or
            # material we should not trust, and both want a human.
            raise ProvenanceError(
                f"npm signature names key {key_id!r}, which {NPM_KEYS_URL} does not publish"
            )
        _verify_npm_signature(spki, signature.get("sig") or "", message)

    return Provenance(
        identity="",
        issuer="",
        predicate="npm-registry-signature",
        mechanism=f"ECDSA P-256 over {name}@{version}:<integrity>, key published by the registry",
    )


def _bundle_from_pep740(attestation: dict) -> bytes:
    """A Sigstore bundle, assembled from PyPI's own envelope.

    PEP 740 does not ship a Sigstore bundle. It ships `{signature, statement}`
    base64 plus `verification_material` holding a Fulcio certificate and
    transparency entries -- and those entries are already in Sigstore's
    protobuf-JSON shape, so the bundle assembles mechanically rather than
    needing a parser of its own. Measured against the live endpoint:
    `Bundle.from_json` accepts the result and `verify_dsse` verifies it.
    """
    material = attestation.get("verification_material") or {}
    certificate = material.get("certificate")
    if not certificate:
        raise ProvenanceError("PyPI attestation carries no certificate")
    envelope = attestation.get("envelope") or {}
    if not envelope.get("statement") or not envelope.get("signature"):
        raise ProvenanceError("PyPI attestation carries an incomplete envelope")

    return json.dumps(
        {
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "verificationMaterial": {
                "certificate": {"rawBytes": certificate},
                "tlogEntries": material.get("transparency_entries") or [],
            },
            "dsseEnvelope": {
                "payload": envelope["statement"],
                "payloadType": _IN_TOTO,
                "signatures": [{"sig": envelope["signature"]}],
            },
        }
    ).encode()


def _publisher_of(bundle_document: dict) -> tuple[str, str]:
    """`(identity, issuer)` read out of the leaf certificate.

    Read from the CERTIFICATE rather than from PyPI's `publisher` block, which
    is unsigned metadata beside the attestation: believing it would record a
    publisher claim that nothing verified.
    """
    from cryptography import x509

    raw = base64.b64decode(bundle_document["verificationMaterial"]["certificate"]["rawBytes"])
    leaf = x509.load_der_x509_certificate(raw)
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    identities = san.get_values_for_type(x509.UniformResourceIdentifier)
    identity = identities[0] if identities else ""

    issuer = ""
    for extension in leaf.extensions:
        # 1.3.6.1.4.1.57264.1.8 is Sigstore's OIDC issuer extension (the V2
        # form); its DER value is a UTF8String, so the payload starts after the
        # tag and length bytes.
        if extension.oid.dotted_string == "1.3.6.1.4.1.57264.1.8":
            decoded = extension.value.value.decode("utf-8", "replace")
            marker = decoded.find("https")
            issuer = decoded[marker:] if marker >= 0 else decoded
    return identity, issuer


async def verify_pypi(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    name: str,
    version: str,
    filename: str,
    digest_hex: str,
) -> Provenance | None:
    """Verify a PEP 740 attestation. None when the file carries none.

    `digest_hex` is checked against the in-toto subject, which is the whole
    point: `verify_dsse` establishes that a given identity signed the envelope
    and says nothing about which artifact the envelope is ABOUT, so a genuine,
    correctly-signed attestation for a different file verifies perfectly. Not
    checking the subject would make substituting one an attack that requires
    forging nothing.
    """
    url = base_url.rstrip("/") + PYPI_PROVENANCE_PATH.format(
        name=name, version=version, filename=filename
    )
    try:
        response = await client.get(url, timeout=30.0)
    except httpx.HTTPError as exc:
        raise ProvenanceUnavailable(f"could not reach {url}: {exc}") from exc

    if response.status_code == 404:
        # The ordinary case for most of PyPI. Not a failure.
        return None
    if response.status_code >= 500:
        raise ProvenanceUnavailable(f"{url} answered {response.status_code}")
    if response.status_code != 200:
        raise ProvenanceUnavailable(f"{url} answered {response.status_code}")

    try:
        document = response.json()
    except ValueError as exc:
        raise ProvenanceError(f"{url} did not return JSON: {exc}") from exc

    bundles = document.get("attestation_bundles") or []
    attestations = [a for b in bundles for a in (b.get("attestations") or [])]
    if not attestations:
        # A 200 with nothing in it. Recorded as "none published" rather than as
        # a failure, because an empty bundle list is what PyPI returns for a
        # project that has provenance for some files and not this one.
        return None

    failures: list[str] = []
    for attestation in attestations:
        try:
            return await _verify_one_pep740(
                attestation, digest_hex=digest_hex, name=name, filename=filename
            )
        except ProvenanceUnavailable:
            raise
        except ProvenanceError as exc:
            failures.append(str(exc))

    # Every attestation present and none verified. Fatal: this is the
    # supply-chain event the check exists to catch, and it does not depend on
    # any configuration being switched on.
    raise ProvenanceError(
        f"{name} {version} {filename} publishes {len(attestations)} PEP 740 "
        f"attestation(s) and none verified: {'; '.join(failures)}"
    )


async def _verify_one_pep740(
    attestation: dict, *, digest_hex: str, name: str, filename: str
) -> Provenance:
    """Verify one attestation, in a thread.

    The Sigstore verification is synchronous and CPU-bound (certificate chain
    building, signature checks) and may refresh the TUF trust root over the
    network, so it must not run on the event loop -- architecture rule 13.
    """
    import asyncio

    bundle_json = _bundle_from_pep740(attestation)
    bundle_document = json.loads(bundle_json)
    identity, issuer = _publisher_of(bundle_document)
    if not identity or not issuer:
        raise ProvenanceError(
            "PEP 740 certificate names no identity/issuer pair, so there is "
            "nothing a policy could ever match on"
        )

    predicate, statement = await asyncio.to_thread(
        _verify_pep740_sync, bundle_json, identity, issuer
    )

    attested = {
        str(entry.get("digest", {}).get("sha256", "")).strip().lower()
        for entry in statement.get("subject") or []
        if isinstance(entry, dict)
    }
    attested.discard("")
    if digest_hex.lower() not in attested:
        raise ProvenanceError(
            f"the attestation verified but is about the wrong artifact: it "
            f"attests {sorted(attested)} and this is {digest_hex.lower()}"
        )

    logger.info(
        "Verified a publisher attestation",
        ecosystem="pypi",
        name=name,
        filename=filename,
        identity=identity,
        issuer=issuer,
        predicate=predicate,
    )
    return Provenance(
        identity=identity, issuer=issuer, predicate=predicate, mechanism="PEP 740 / Sigstore"
    )


def _verify_pep740_sync(bundle_json: bytes, identity: str, issuer: str) -> tuple[str, dict]:
    """The blocking half. Returns `(predicateType, statement)`."""
    from terrapod.services import sigstore_verification as sigver

    try:
        from sigstore.models import Bundle
        from sigstore.verify import Verifier, policy
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ProvenanceUnavailable(f"sigstore is not available: {exc}") from exc

    try:
        trust = sigver._trust_config(sigver.DEFAULT_TUF_URL, refresh=True, ttl_seconds=3600.0)
    except sigver.TrustRootUnavailable as exc:
        raise ProvenanceUnavailable(str(exc)) from exc

    try:
        bundle = Bundle.from_json(bundle_json)
    except Exception as exc:
        raise ProvenanceError(f"PyPI attestation is not a usable bundle: {exc}") from exc

    verifier = Verifier(trusted_root=trust.trusted_root)
    try:
        payload_type, payload = verifier.verify_dsse(
            bundle=bundle, policy=policy.Identity(identity=identity, issuer=issuer)
        )
    except Exception as exc:
        raise ProvenanceError(f"PEP 740 attestation did not verify: {exc}") from exc

    if payload_type != _IN_TOTO:
        raise ProvenanceError(
            f"the envelope verified but carries {payload_type!r}, not in-toto, "
            "so it has no subject to check the artifact against"
        )
    try:
        statement = json.loads(payload)
    except ValueError as exc:
        raise ProvenanceError(f"in-toto statement is not JSON: {exc}") from exc
    return str(statement.get("predicateType") or ""), statement
