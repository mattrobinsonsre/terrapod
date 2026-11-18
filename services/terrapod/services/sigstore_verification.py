"""Sigstore verification for artifacts whose publisher signs with cosign (#2589).

``artifact_verification`` covers the kinds whose publisher ships a GPG-signed
``SHA256SUMS``: the engine binaries and provider archives. A second group signs
with **Sigstore** instead, and for them GPG is not a weaker option but an absent
one -- OCI content is signed with cosign, and PyPI and npm publish
Sigstore-backed attestations. Decision 6 on #2589 sets the standard as per-kind
best-available material, so those kinds need this module rather than a note
explaining why they are unverified.

What Sigstore verification actually establishes, since it is a different claim
from a GPG signature rather than the same claim by other means:

- the artifact's digest is covered by a signature made by a **short-lived
  certificate** issued by Fulcio;
- that certificate chains to a Fulcio root we trust, and its subject names an
  **identity** -- a workflow, a repository, an email -- which is the thing worth
  pinning. A valid signature by an identity we did not expect is a failure here,
  not a pass;
- the signing event is in Rekor's transparency log, so it cannot have been made
  quietly.

The identity is the whole point and the easiest thing to get wrong: verifying
that a bundle is *internally* valid while accepting any signer at all would
admit an artifact signed by anybody who can get a Fulcio certificate, which is
anybody. So an identity policy is **required** by this module's API rather than
defaulted -- see ``verify_bundle``.

**The trust root is a second upstream, and it does not need mirroring here.**
Fulcio's chain and Rekor's key arrive over TUF from
``tuf-repo-cdn.sigstore.dev``, which is the shape ``GOSUMDB`` had before #2614:
a control that looks like it must be switched off because a sealed deployment
cannot reach it. It does not, for the reason the OCI pull-through states in its
own header -- "in a sealed deployment a miss is an error, never an upstream
request". Admission-time verification happens on a **miss**, which is the one
moment the deployment is fetching anything at all and therefore has egress. The
artifact and the trust root are fetched in the same breath.

What that leaves, stated rather than discovered later: **re-verifying** an
already-cached artifact after a deployment is sealed has no trust root to reach.
``refresh=False`` uses the persisted TUF cache for exactly that case, and a root
cached before a key rotation will refuse a signature made after it -- which is
why the failure is reported as a distinct reason rather than as a bad artifact.
Mirroring the TUF repository through the cache is the fix if that case ever
matters; it is deliberately not built yet, because nothing re-verifies today.

``sigstore``'s verification is synchronous and does network I/O (Rekor, and a
TUF refresh), so every entry point here dispatches to a worker thread -- the
HARD invariant against sync work in an ``async`` handler.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

logger = structlog.get_logger(__name__)

#: Where the production Sigstore trust root is published. Overridable so an
#: internal-mirror deployment can point at its own TUF repository, exactly as
#: `go.sumdb_upstream` lets one point at a mirrored checksum database.
DEFAULT_TUF_URL = "https://tuf-repo-cdn.sigstore.dev"


class SigstoreVerificationError(Exception):
    """Raised when an artifact fails Sigstore verification.

    Fail-closed: the caller must not cache or serve the artifact. The message
    distinguishes *why* so that an operator can tell a bad artifact from an
    environmental problem -- a stale trust root and a forged signature are both
    "verification failed" and want opposite responses.
    """


class TrustRootUnavailable(SigstoreVerificationError):
    """The trust root could not be obtained, so nothing could be verified.

    Separate from a verification failure because it says nothing about the
    artifact. A sealed deployment re-verifying cached content reaches this, and
    the answer is to mirror the TUF repository rather than to distrust the
    artifact.
    """


@dataclass(frozen=True, slots=True)
class ExpectedSigner:
    """Who must have signed, and which issuer vouched for that identity.

    Both halves are needed. An identity without an issuer is forgeable by
    anyone who can obtain a certificate naming that identity from *some* OIDC
    provider, so `sigstore`'s own `policy.Identity` takes the issuer too and
    this mirrors it rather than inventing a looser shape.

    `ci.yml` pins its own images the same way -- `--certificate-identity-regexp`
    plus `--certificate-oidc-issuer` (#549, #626) -- so the discipline has an
    in-tree precedent rather than being introduced here.
    """

    identity: str
    issuer: str

    def __post_init__(self) -> None:
        if not self.identity.strip():
            raise ValueError("an expected signer needs an identity")
        if not self.issuer.strip():
            raise ValueError(
                "an expected signer needs an issuer: an identity alone is forgeable by "
                "anyone who can get a certificate naming it from any OIDC provider"
            )


def _checked_digest(digest_hex: str) -> bytes:
    """Validate a hex sha256 and return its bytes, importing nothing.

    Deliberately free of any `sigstore` import so that a malformed digest is
    refused even where the library is absent, and -- the reason it is a
    separate function -- so the refusal cannot be reordered behind either the
    import or the TUF refresh by a later edit. Both have happened: the first
    version of this module put the ~3s refresh first, and the second still had
    the library import above this check.
    """
    want = digest_hex.strip().lower()
    try:
        raw = bytes.fromhex(want)
    except ValueError as exc:
        raise SigstoreVerificationError(f"{digest_hex!r} is not a hex sha256 digest") from exc
    if len(raw) != 32:
        raise SigstoreVerificationError(
            f"a sha256 digest is 32 bytes; got {len(raw)} from {digest_hex!r}"
        )
    return raw


# The trust root is cached because obtaining it is a TUF refresh -- several
# network round trips and signature checks -- and admission can verify many
# artifacts in a burst (a `docker pull` of one image fetches a manifest and
# every layer). It is cached with a BOUND rather than for ever, because the
# root is the one mutable thing in this picture: Sigstore rotates keys, and a
# root held indefinitely would start refusing valid signatures. That is the
# same rule the provider version index and the tofu version list follow -- an
# immutable artifact needs no expiry, a listing of what currently exists always
# does.
_TRUST_CACHE: dict[str, tuple[float, object]] = {}


def _trust_config(url: str, *, refresh: bool, ttl_seconds: float):
    """Obtain a `ClientTrustConfig`, from the cache when it is still fresh.

    `refresh=False` is the sealed-deployment path: use whatever the local TUF
    cache already holds and make no network request. It is the honest answer
    there rather than a faster one -- see the module docstring on why a root
    cached before a rotation reports its own reason.
    """
    import time

    from sigstore.models import ClientTrustConfig

    cached = _TRUST_CACHE.get(url)
    if cached is not None and time.monotonic() - cached[0] < ttl_seconds:
        return cached[1]

    try:
        config = ClientTrustConfig.from_tuf(url, offline=not refresh)
    except Exception as exc:  # noqa: BLE001 - the library raises several kinds
        # Deliberately not falling back to a stale cached root on a refresh
        # failure. A root we could not refresh is a root we cannot vouch for,
        # and silently verifying against yesterday's is how an expired
        # revocation goes unnoticed. The caller gets a reason that names the
        # environment rather than the artifact.
        raise TrustRootUnavailable(
            f"could not obtain the Sigstore trust root from {url}: {exc}"
        ) from exc

    _TRUST_CACHE[url] = (time.monotonic(), config)
    return config


def _verify_sync(
    *,
    digest_hex: str,
    bundle_json: bytes,
    expect: ExpectedSigner,
    tuf_url: str,
    refresh: bool,
    ttl_seconds: float,
) -> None:
    """The synchronous body. Never call this from an `async` handler."""
    # The digest first, before even the import: it needs nothing, and a bad
    # one reported as "could not obtain the trust root" (or as a missing
    # library) sends an operator to look at their egress instead of at the
    # caller.
    digest = _checked_digest(digest_hex)

    from sigstore.hashes import HashAlgorithm, Hashed
    from sigstore.models import Bundle
    from sigstore.verify import Verifier, policy

    try:
        bundle = Bundle.from_json(bundle_json)
    except Exception as exc:  # noqa: BLE001
        raise SigstoreVerificationError(f"the Sigstore bundle did not parse: {exc}") from exc

    config = _trust_config(tuf_url, refresh=refresh, ttl_seconds=ttl_seconds)
    verifier = Verifier(trusted_root=config.trusted_root)
    want = policy.Identity(identity=expect.identity, issuer=expect.issuer)

    try:
        verifier.verify_artifact(
            Hashed(algorithm=HashAlgorithm.SHA2_256, digest=digest),
            bundle,
            want,
        )
    except Exception as exc:  # noqa: BLE001 - sigstore raises VerificationError
        raise SigstoreVerificationError(
            f"the signature over sha256:{digest_hex} did not verify as "
            f"{expect.identity} via {expect.issuer}: {exc}"
        ) from exc


async def verify_digest(
    *,
    digest_hex: str,
    bundle_json: bytes,
    expect: ExpectedSigner,
    tuf_url: str = DEFAULT_TUF_URL,
    refresh: bool = True,
    ttl_seconds: float = 3600.0,
) -> None:
    """Verify that `digest_hex` is signed by `expect`, or raise.

    Takes the **digest** rather than the bytes, which is what makes this usable
    on OCI content: a layer can be hundreds of megabytes, the API pod's `/tmp`
    is RAM-backed, and `sigstore` accepts a prehashed input precisely so a
    verifier need not hold the artifact. Nothing here buffers the subject.

    `expect` is required. There is no "any valid signature" mode, because a
    Fulcio certificate is obtainable by anybody -- so a bundle that verifies
    against no particular identity establishes only that *someone* signed
    something, which is not a control. See `ExpectedSigner`.

    Raises `TrustRootUnavailable` when the trust root could not be obtained
    (which says nothing about the artifact) and `SigstoreVerificationError`
    when the artifact genuinely failed. Fail-closed: the caller caches nothing
    and serves nothing on either.
    """
    await asyncio.to_thread(
        _verify_sync,
        digest_hex=digest_hex,
        bundle_json=bundle_json,
        expect=expect,
        tuf_url=tuf_url,
        refresh=refresh,
        ttl_seconds=ttl_seconds,
    )
    logger.info(
        "Sigstore verification passed",
        digest=f"sha256:{digest_hex}",
        identity=expect.identity,
        issuer=expect.issuer,
    )


#: The payload type a Sigstore DSSE envelope carries for an in-toto statement.
#: `verify_dsse` hands back the type alongside the payload precisely so the
#: caller checks it before trusting the bytes, and a bundle is free to carry
#: something else.
_IN_TOTO = "application/vnd.in-toto+json"


def _attested_digests(statement: dict) -> set[str]:
    """The sha256 digests an in-toto statement claims to be *about*.

    An in-toto subject is a list, each entry carrying a `digest` map keyed by
    algorithm, so a statement can legitimately cover several artifacts. Only
    sha256 is read here because that is the only algorithm the callers address
    content by.
    """
    out: set[str] = set()
    for subject in statement.get("subject") or []:
        if not isinstance(subject, dict):
            continue
        sha256 = (subject.get("digest") or {}).get("sha256")
        if isinstance(sha256, str) and sha256.strip():
            out.add(sha256.strip().lower())
    return out


def _verify_attestation_sync(
    *,
    digest_hex: str,
    bundle_json: bytes,
    expect: ExpectedSigner,
    tuf_url: str,
    refresh: bool,
    ttl_seconds: float,
) -> tuple[str, dict]:
    """The synchronous body. Never call this from an `async` handler."""
    # The digest first, before even the import -- see `_checked_digest`.
    _checked_digest(digest_hex)
    want_digest = digest_hex.strip().lower()

    from sigstore.models import Bundle
    from sigstore.verify import Verifier, policy

    try:
        bundle = Bundle.from_json(bundle_json)
    except Exception as exc:  # noqa: BLE001
        raise SigstoreVerificationError(f"the Sigstore bundle did not parse: {exc}") from exc

    config = _trust_config(tuf_url, refresh=refresh, ttl_seconds=ttl_seconds)
    verifier = Verifier(trusted_root=config.trusted_root)
    want = policy.Identity(identity=expect.identity, issuer=expect.issuer)

    try:
        payload_type, payload = verifier.verify_dsse(bundle, want)
    except Exception as exc:  # noqa: BLE001
        raise SigstoreVerificationError(
            f"the attestation over sha256:{want_digest} did not verify as "
            f"{expect.identity} via {expect.issuer}: {exc}"
        ) from exc

    if payload_type != _IN_TOTO:
        raise SigstoreVerificationError(
            f"the envelope verified but carries {payload_type!r} rather than {_IN_TOTO!r}, "
            "so its payload is not an in-toto statement and its subject cannot be checked"
        )

    import json as _json

    try:
        statement = _json.loads(payload)
    except ValueError as exc:
        raise SigstoreVerificationError(f"the verified in-toto payload is not JSON: {exc}") from exc
    if not isinstance(statement, dict):
        raise SigstoreVerificationError("the verified in-toto payload is not an object")

    # THE check this function exists for. `verify_dsse` establishes that the
    # expected identity signed THIS envelope; it says nothing about which
    # artifact the envelope is about. A genuine, correctly-signed attestation
    # for a different image verifies perfectly here -- so without this, an
    # attacker who can choose which bundle we fetch substitutes one they did
    # not have to forge. Checking the subject is what ties the signature to the
    # bytes we are admitting.
    attested = _attested_digests(statement)
    if want_digest not in attested:
        raise SigstoreVerificationError(
            f"the attestation verified as {expect.identity} but is about "
            f"{sorted(attested) or ['nothing']}, not sha256:{want_digest} -- "
            "a valid signature over the wrong artifact"
        )

    return str(statement.get("predicateType") or ""), statement


async def verify_attestation(
    *,
    digest_hex: str,
    bundle_json: bytes,
    expect: ExpectedSigner,
    tuf_url: str = DEFAULT_TUF_URL,
    refresh: bool = True,
    ttl_seconds: float = 3600.0,
) -> tuple[str, dict]:
    """Verify a DSSE attestation and that it is **about** `digest_hex`.

    This is the path real cosign material takes, which is worth stating because
    the other one looks like the obvious choice: measured against our own
    published images, `cosign download signature` returns
    `application/vnd.dev.sigstore.bundle.v0.3+json` envelopes carrying
    `dsseEnvelope` rather than a bare message signature -- three of them, for
    `sigstore.dev/cosign/sign/v1`, `slsa.dev/provenance/v1` and
    `spdx.dev/Document/v2.3`. So an OCI caller wants this, not `verify_digest`.

    Returns `(predicateType, statement)` so a caller can tell an SBOM
    attestation from a provenance one without parsing the payload again.

    Raises `SigstoreVerificationError` when the signature fails, when the
    envelope carries something other than an in-toto statement, **or when the
    statement is about a different artifact** -- see the comment on that last
    check, which is the one a caller would otherwise forget.
    """
    predicate, statement = await asyncio.to_thread(
        _verify_attestation_sync,
        digest_hex=digest_hex,
        bundle_json=bundle_json,
        expect=expect,
        tuf_url=tuf_url,
        refresh=refresh,
        ttl_seconds=ttl_seconds,
    )
    logger.info(
        "Sigstore attestation verified",
        digest=f"sha256:{digest_hex.strip().lower()}",
        identity=expect.identity,
        predicate=predicate,
    )
    return predicate, statement
