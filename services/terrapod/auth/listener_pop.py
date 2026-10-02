"""Proof of possession for listener-authenticated requests.

A listener authenticates with `X-Terrapod-Client-Cert`: the base64 PEM of the
certificate the CA issued it at join. A certificate is **public material** and it
travels on every request, so on its own it is a bearer credential — anyone who
observes one request can replay that header until the certificate expires, and
every check `get_listener_identity` performs (CA signature, expiry, CN lookup,
fingerprint match) is satisfied just as well by the copy as by the holder.

This module closes that gap by requiring the caller to prove it holds the private
key matching the certificate. The CA returns that key to the listener once, at
join, and the listener stores it alongside the certificate.

What is signed, and why the body is not part of it
--------------------------------------------------
The signed string is::

    v1\\n<METHOD>\\n<path>\\n<timestamp>\\n<nonce>

The request body is **deliberately excluded**, and that is a decision rather than
an omission. The weakness being closed is *replay of a credential*, not tampering
with a payload: TLS already protects integrity in transit, and the signature
binds the request to one method, one path and one single-use nonce inside a
narrow window, so a captured signature cannot be redirected at another endpoint
or used twice.

Including a body digest would mean buffering the whole body inside an async
dependency in order to hash it — and the listener PUTs pod logs through this
path. Architecture rules 13 and 14 exist to stop exactly that, so adding a body
hash here would trade a fixed replay window for an event-loop stall and a
memory-backed buffer on every log upload. If someone later wants body binding,
it belongs in a streaming hash at the storage boundary, not here.
"""

from __future__ import annotations

import base64
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

#: Seconds either side of now that a timestamp may fall. Generous enough for the
#: clock skew a remote execution cluster really has, tight enough that a captured
#: signature is useless almost immediately. The nonce is what actually prevents
#: reuse inside the window; this only bounds how long a nonce must be remembered.
CLOCK_SKEW_SECONDS = 60

#: Redis key for a spent nonce. Held for twice the window so a nonce cannot come
#: back as the window slides.
_NONCE_KEY = "tp:listener_pop:{nonce}"
_NONCE_TTL = CLOCK_SKEW_SECONDS * 2

SIGNATURE_HEADER = "X-Terrapod-Listener-Signature"
TIMESTAMP_HEADER = "X-Terrapod-Listener-Timestamp"
NONCE_HEADER = "X-Terrapod-Listener-Nonce"


def canonical_request(method: str, path: str, timestamp: str, nonce: str) -> bytes:
    """The exact bytes both sides sign over.

    `method` is upper-cased and `path` is taken verbatim, so a signature minted
    for `GET /runs/next` cannot be presented on `POST /runs/next` or on another
    path. There is one canonicalisation and both sides call it — a second,
    subtly different copy on either side is how a scheme like this silently
    starts accepting everything or nothing.
    """
    return "\n".join(("v1", method.upper(), path, timestamp, nonce)).encode()


def sign_request(private_key_pem: str, method: str, path: str, timestamp: str, nonce: str) -> str:
    """Sign a request as the listener. Returns base64 of the raw signature."""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    sig = key.sign(canonical_request(method, path, timestamp, nonce))
    return base64.b64encode(sig).decode()


class ProofOfPossessionError(Exception):
    """Verification failed. The message is safe to return to the caller: it
    describes which rule was broken, never any key or signature material."""


def _check_timestamp(timestamp: str, *, now: float | None = None) -> None:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise ProofOfPossessionError("timestamp is not an integer") from None
    current = time.time() if now is None else now
    if abs(current - ts) > CLOCK_SKEW_SECONDS:
        raise ProofOfPossessionError(
            f"timestamp is outside the permitted {CLOCK_SKEW_SECONDS}s window"
        )


async def _claim_nonce(nonce: str) -> None:
    """Spend the nonce, or refuse.

    `SET NX` is the whole mechanism: the first caller to present a nonce claims
    it, and every later presentation of the same one is a replay. Single key, so
    this is safe on a Redis cluster.
    """
    if not nonce or len(nonce) < 16:
        raise ProofOfPossessionError("nonce missing or too short")
    from terrapod.redis.client import get_redis_client

    claimed = await get_redis_client().set(
        _NONCE_KEY.format(nonce=nonce), "1", nx=True, ex=_NONCE_TTL
    )
    if not claimed:
        raise ProofOfPossessionError("nonce has already been used")


async def verify_request(
    cert,
    *,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    signature: str,
) -> None:
    """Raise `ProofOfPossessionError` unless the caller holds the private key.

    Order matters: the cheap, stateless checks run before the Redis round trip,
    and the nonce is spent only once the signature has verified. Spending it
    first would let an unauthenticated caller burn a victim's nonce and have the
    victim's own retry rejected as a replay.
    """
    _check_timestamp(timestamp)
    try:
        raw = base64.b64decode(signature, validate=True)
    except Exception:
        raise ProofOfPossessionError("signature is not valid base64") from None
    try:
        cert.public_key().verify(raw, canonical_request(method, path, timestamp, nonce))
    except (InvalidSignature, Exception) as exc:
        if isinstance(exc, InvalidSignature):
            raise ProofOfPossessionError("signature does not match this certificate") from None
        raise ProofOfPossessionError("signature could not be verified") from None
    await _claim_nonce(nonce)
