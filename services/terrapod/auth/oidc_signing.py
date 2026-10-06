"""The signing key behind Terrapod's OIDC issuer for runs (#1901).

Terrapod mints a short-lived RS256 JWT per run whose claims describe the run —
which workspace, which phase — and a cloud federates to Terrapod as an identity
provider rather than to a ServiceAccount shared by every run on an agent pool.
This module owns the keypair that makes those tokens worth anything.

**RS256, not the CA's Ed25519.** The listener CA is the only other asymmetric key
Terrapod owns and reusing it is tempting, but at least one major cloud does not
accept EdDSA for workload identity federation, and a federation path that works
on two clouds out of three is not worth the elegance. So this is a second trust
root beside the CA, with the same lifecycle.

**Key management follows `auth.ca`, for the same reason it does there.** The
database is the single source of truth, loaded on every startup, and the
check-then-create is serialised across replicas with a transaction-scoped
Postgres advisory lock (#1060). Without that two replicas starting against a
fresh database each generate a key, each insert it, and the fleet then signs with
two different keys while publishing whichever each replica happens to hold — and
for a *published* trust root that is worse than for the CA, because the failure
surfaces as a cloud rejecting a token with nothing wrong on our side to look at.

**Never chart-generated.** Under `helm template` — which is what Argo CD and Flux
run — `lookup` returns nothing and `.Release.IsInstall` is always true, so a
generating branch re-mints on every render. For this key that would break every
federated workspace in the deployment at once, on a schedule nobody chose.

**An operator's own key wins on every startup and is never stored.** Copying it
into the table once would mean the first value ever supplied wins for ever and
every later rotation is silently ignored — the flag-nothing-reads defect in a
more expensive form (#1994). A BYO deployment therefore never touches this table.

**Rotation is a set, not a swap**, because a published trust root cannot be
swapped atomically: the clouds fetch the JWKS on their own schedule and cache it.
So a rotation adds a key, which is published immediately and signs only once the
propagation window has passed; and a retired key stays published for a grace
window, because tokens it already signed are still inside their own TTL. Both
windows are configuration, because only the operator knows how long their clouds
cache.
"""

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

#: Stable, arbitrary key for the Postgres advisory lock serialising issuer-key
#: initialisation across replicas. Distinct from the CA's (#1060) — two different
#: singletons must not queue behind each other.
_OIDC_KEY_INIT_ADVISORY_LOCK = 776699001133

#: 2048 is the floor every cloud accepts and the size every cloud's docs use.
#: 4096 buys nothing here: the token lives minutes and the key rotates.
_RSA_KEY_SIZE = 2048

_RSA_PUBLIC_EXPONENT = 65537


@dataclass(frozen=True)
class SigningKey:
    """One key in the set, with the two facts a caller needs about it."""

    kid: str
    private_key_pem: str
    #: None for an operator-supplied key, which has no row and cannot be rotated
    #: by us.
    row_id: uuid.UUID | None = None


#: Process-wide cache of the resolved set. Rebuilt by `init_oidc_signing` and by
#: a rotation; never stale for longer than one of those, because every read goes
#: through `get_signing_key`/`get_jwks` which raise when nothing is loaded rather
#: than silently signing with a key the database no longer agrees with.
_keys: list[SigningKey] | None = None
_signing_kid: str | None = None

#: The published JWKS, memoised against the set of `kid`s it was built from.
#:
#: Rendering it means parsing every private key PEM, and the discovery and JWKS
#: documents are the two endpoints a deployment must expose publicly for
#: federation to work at all -- so without this an unauthenticated caller can
#: make the API do RSA key parsing at whatever rate they like. The cache key is
#: the tuple of `kid`s, which is sound rather than approximate: a `kid` is an
#: RFC 7638 thumbprint DERIVED from the key material, so identical kids mean
#: identical keys and changed material cannot reuse a kid.
_jwks_cache: tuple[tuple[str, ...], dict[str, list[dict[str, str]]]] | None = None


def generate_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_RSA_KEY_SIZE)


def serialize_private_key(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def load_private_key(pem: str) -> rsa.RSAPrivateKey:
    """Load a PKCS8/PKCS1 PEM, refusing anything that is not an RSA key.

    An operator who supplies an Ed25519 or EC key gets a named error at startup
    rather than a JWKS the clouds silently cannot use.
    """
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            f"OIDC issuer signing key must be RSA (RS256), got {type(key).__name__}. "
            "EdDSA and EC keys are not accepted by every cloud's workload identity "
            "federation, so Terrapod does not offer them."
        )
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_to_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    """The public half as a JWK, ready to publish."""
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_to_b64url(numbers.n),
        "e": _int_to_b64url(numbers.e),
    }


def compute_kid(key: rsa.RSAPrivateKey) -> str:
    """RFC 7638 JWK thumbprint.

    Derived from the key rather than assigned, so it is stable across restarts,
    cannot collide, and is computed identically for a key we generated and a key
    an operator supplied. The canonical form is the required members only, in
    lexicographic order, with no whitespace — which is why this does not reuse
    `public_jwk`.
    """
    numbers = key.public_key().public_numbers()
    canonical = json.dumps(
        {"e": _int_to_b64url(numbers.e), "kty": "RSA", "n": _int_to_b64url(numbers.n)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def _configured_key_pem() -> str | None:
    """The operator's own key, or None.

    Read on every startup and never written anywhere, which is the whole point:
    rotating a BYO key is something the operator does to their own secret, and
    storing a copy would make the first value supplied win for ever.
    """
    from terrapod.config import settings

    pem = (getattr(settings.auth.oidc_issuer, "signing_key_pem", "") or "").strip()
    return pem or None


async def init_oidc_signing(db: AsyncSession) -> list[SigningKey]:
    """Resolve the signing set at startup: operator's key, else the database.

    Generates and persists one when the table is empty, under the advisory lock
    so concurrent replicas queue rather than each creating their own.
    """
    from terrapod.config import settings
    from terrapod.db.models import OIDCSigningKey

    global _keys, _signing_kid  # noqa: PLW0603

    configured = _configured_key_pem()
    if configured is not None:
        key = load_private_key(configured)
        kid = compute_kid(key)
        _keys = [SigningKey(kid=kid, private_key_pem=configured)]
        _signing_kid = kid
        logger.info("OIDC issuer using the operator-supplied signing key", kid=kid)
        return _keys

    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _OIDC_KEY_INIT_ADVISORY_LOCK})

    rows = (
        (await db.execute(select(OIDCSigningKey).order_by(OIDCSigningKey.created_at.asc())))
        .scalars()
        .all()
    )

    if not rows:
        key = generate_private_key()
        kid = compute_kid(key)
        row = OIDCSigningKey(kid=kid, private_key_pem=serialize_private_key(key))
        db.add(row)
        await db.commit()
        rows = [row]
        logger.info("Generated and stored a new OIDC issuer signing key", kid=kid)
    else:
        # Release the advisory lock (held on this transaction) now the read is
        # done; nothing was written.
        await db.commit()

    # Grace-filter here too. `init` reads every row, and a key retired longer ago
    # than the window is no longer published, so it must not be selectable.
    grace = settings.auth.oidc_issuer.retired_key_grace_seconds
    live_cutoff = datetime.now(UTC) - timedelta(seconds=grace)
    live = [r for r in rows if r.retired_at is None or r.retired_at > live_cutoff]
    _keys = [SigningKey(kid=r.kid, private_key_pem=r.private_key_pem, row_id=r.id) for r in live]
    _signing_kid = _choose_signing_kid(live, grace_seconds=grace)
    logger.info(
        "Loaded OIDC issuer signing keys",
        count=len(_keys),
        signing_kid=_signing_kid,
    )
    return _keys


def _choose_signing_kid(rows: list, *, grace_seconds: int) -> str:
    """Which key signs right now, preferring one the clouds can already verify.

    Three tiers, in this order, and the middle one is the whole point:

    1. **Active and not retired** — the normal case. Newest wins.
    2. **Retired but still inside its grace window.** This is what holds during
       a rotation's propagation window, when tier 1 is empty BY CONSTRUCTION:
       the incoming key is published but not yet active, and the outgoing key
       was retired the moment the rotation happened. A retired key is still in
       the published JWKS until its grace expires, so tokens it signs verify —
       whereas the new key is precisely the one the clouds have not fetched yet.
       So keep signing with the old one until the new one has propagated.
    3. **Newest unretired, whatever its activation.** A genuine last resort, and
       warned about: every key is retired past its grace, or the only key has a
       future activation. Signing with a key the clouds may not hold beats not
       signing at all, but an operator needs to know.

    Tier 2 is why this takes the grace window rather than reading config: a key
    retired longer ago than that is no longer published, so signing with it
    would produce tokens nothing can verify — tier 3's failure with none of its
    warning.

    Getting tiers 2 and 3 the wrong way round made `key_propagation_seconds`
    dead in the only path that ever produces a not-yet-active key, so every
    rotation was a hard cutover while the warning blamed the operator for a
    state `rotate_signing_key` creates every time. Found by running it.
    """
    now = datetime.now(UTC)

    active = [r for r in rows if r.retired_at is None and r.activates_at <= now]
    if active:
        return max(active, key=lambda r: r.created_at).kid

    cutoff = now - timedelta(seconds=grace_seconds)
    still_published = [r for r in rows if r.retired_at is not None and r.retired_at > cutoff]
    if still_published:
        chosen = max(still_published, key=lambda r: r.retired_at)
        logger.info(
            "Signing with the previous OIDC key while the incoming one propagates",
            kid=chosen.kid,
        )
        return chosen.kid

    unretired = [r for r in rows if r.retired_at is None]
    if not unretired:
        raise RuntimeError(
            "Every OIDC issuer signing key is retired and past its grace window. "
            "Rotate to create one, or supply "
            "api.config.auth.oidc_issuer.signing_key_pem."
        )
    chosen = max(unretired, key=lambda r: r.created_at)
    logger.warning(
        "No OIDC signing key is active and no retired key is still published; "
        "signing with the newest even though the clouds may not hold it yet",
        kid=chosen.kid,
    )
    return chosen.kid


def get_signing_key() -> SigningKey:
    """The key to sign with. Raises when nothing has been initialised."""
    if _keys is None or _signing_kid is None:
        raise RuntimeError(
            "OIDC issuer signing key not initialised — init_oidc_signing() runs in "
            "the app lifespan."
        )
    for k in _keys:
        if k.kid == _signing_kid:
            return k
    raise RuntimeError(f"OIDC signing key {_signing_kid} is not in the loaded set")


def get_jwks() -> dict[str, list[dict[str, str]]]:
    """The published key set.

    Everything currently loaded, which is the current key plus any retired key
    still inside its grace window — a token signed before a rotation has to keep
    verifying until it expires.

    Memoised: this is served unauthenticated by necessity, and building it parses
    every private key PEM. See `_jwks_cache` for why the kid tuple is a sound
    cache key. The returned dict is the cached one and callers must not mutate
    it — the route serialises it and does not.
    """
    global _jwks_cache  # noqa: PLW0603

    if _keys is None:
        raise RuntimeError(
            "OIDC issuer signing key not initialised — init_oidc_signing() runs in "
            "the app lifespan."
        )

    kids = tuple(k.kid for k in _keys)
    if _jwks_cache is not None and _jwks_cache[0] == kids:
        return _jwks_cache[1]

    jwks = {"keys": [public_jwk(load_private_key(k.private_key_pem), k.kid) for k in _keys]}
    _jwks_cache = (kids, jwks)
    return jwks


async def rotate_signing_key(db: AsyncSession) -> SigningKey:
    """Add a key, and retire the one currently signing.

    Published immediately and signing only after the propagation window, so no
    token is ever signed with a key the clouds have had no chance to fetch. The
    retired key stays published for its grace window.

    Refused for a BYO deployment: the key is the operator's and so is rotating it.
    """
    from terrapod.config import settings
    from terrapod.db.models import OIDCSigningKey

    global _keys, _signing_kid  # noqa: PLW0603

    if _configured_key_pem() is not None:
        raise ValueError(
            "This deployment signs with an operator-supplied key "
            "(api.config.auth.oidc_issuer.signing_key_pem), so Terrapod does not "
            "rotate it. Replace the secret and restart the API."
        )

    cfg = settings.auth.oidc_issuer
    now = datetime.now(UTC)

    key = generate_private_key()
    kid = compute_kid(key)
    row = OIDCSigningKey(
        kid=kid,
        private_key_pem=serialize_private_key(key),
        activates_at=now + timedelta(seconds=cfg.key_propagation_seconds),
    )
    db.add(row)

    # Retire whatever is signing now. It keeps verifying for the grace window.
    current = get_signing_key()
    if current.row_id is not None:
        existing = await db.get(OIDCSigningKey, current.row_id)
        if existing is not None and existing.retired_at is None:
            existing.retired_at = now

    await db.commit()
    await reload_signing_keys(db)
    logger.info(
        "Rotated the OIDC issuer signing key",
        new_kid=kid,
        retired_kid=current.kid,
        signs_from=row.activates_at.isoformat(),
    )
    return SigningKey(kid=kid, private_key_pem=row.private_key_pem, row_id=row.id)


async def reload_signing_keys(db: AsyncSession) -> list[SigningKey]:
    """Re-read the set, dropping keys past their grace window.

    Called after a rotation and by the periodic task, which is what lets a
    rotation on one replica reach the others without a restart.
    """
    from terrapod.config import settings
    from terrapod.db.models import OIDCSigningKey

    global _keys, _signing_kid  # noqa: PLW0603

    if _configured_key_pem() is not None:
        return _keys or []

    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.auth.oidc_issuer.retired_key_grace_seconds
    )
    rows = (
        (await db.execute(select(OIDCSigningKey).order_by(OIDCSigningKey.created_at.asc())))
        .scalars()
        .all()
    )
    live = [r for r in rows if r.retired_at is None or r.retired_at > cutoff]
    if not live:
        raise RuntimeError("No live OIDC issuer signing key after reload")

    _keys = [SigningKey(kid=r.kid, private_key_pem=r.private_key_pem, row_id=r.id) for r in live]
    _signing_kid = _choose_signing_kid(
        live, grace_seconds=settings.auth.oidc_issuer.retired_key_grace_seconds
    )
    return _keys


def _reset_for_tests() -> None:
    global _keys, _signing_kid, _jwks_cache  # noqa: PLW0603
    _keys = None
    _signing_kid = None
    _jwks_cache = None


def sign_identity_token(claims: dict, *, ttl_seconds: int) -> str:
    """Sign run identity claims as an RS256 JWT.

    `iat`/`exp`/`jti` are set here rather than by the caller: a caller that
    forgot one would mint a token with no expiry, and `jti` is what makes two
    tokens for the same run distinguishable in a cloud audit log.
    """
    import jwt

    key = get_signing_key()
    now = int(time.time())
    payload = {
        **claims,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, key.private_key_pem, algorithm="RS256", headers={"kid": key.kid})
