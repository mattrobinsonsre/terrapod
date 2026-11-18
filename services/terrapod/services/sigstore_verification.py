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
    from sigstore.hashes import HashAlgorithm, Hashed
    from sigstore.models import Bundle
    from sigstore.verify import Verifier, policy

    # Local checks first, deliberately. Obtaining the trust root is a TUF
    # refresh -- measured at ~3s against the real repository -- and a malformed
    # digest or an unparseable bundle is knowable without touching the network.
    # Doing it the other way round also mislabels the failure: a bad digest
    # reported as "could not obtain the trust root" sends an operator to look
    # at their egress instead of at the caller.
    try:
        digest = bytes.fromhex(digest_hex)
    except ValueError as exc:
        raise SigstoreVerificationError(f"{digest_hex!r} is not a hex sha256 digest") from exc
    if len(digest) != 32:
        raise SigstoreVerificationError(
            f"a sha256 digest is 32 bytes; got {len(digest)} from {digest_hex!r}"
        )

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
