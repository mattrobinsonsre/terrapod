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

import asyncio
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

#: Stable, arbitrary key for the Postgres advisory lock serialising WRITES to the
#: issuer-key table across replicas -- initialisation and rotation alike. One lock
#: for both on purpose: they contend for the same rows, and a rotation racing an
#: init is the same hazard as two rotations racing. Distinct from the CA's (#1060)
#: — two different singletons must not queue behind each other.
_OIDC_KEY_WRITE_ADVISORY_LOCK = 776699001133

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
    #: When this key was retired, carried so a READ can drop it once its grace
    #: window passes rather than relying on the next successful reload to do it.
    #: A replica that cannot reach the database holds this set indefinitely, and
    #: without the read-side filter it would keep publishing -- and signing with
    #: -- a key the operator revoked during an incident. None for a key that was
    #: never retired, and for an operator-supplied key.
    retired_at: datetime | None = None


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

#: Prepared private keys, by kid. `jwt.encode` given a PEM STRING re-parses it on
#: every call, and the mint signs once per provider configuration a run uses --
#: up to `MAX_TARGETS` of them in one request, synchronously, on the event loop.
#: A kid is a thumbprint OF the key material, so it cannot name two different
#: keys -- so an entry for a key no longer in the set is stale but never WRONG,
#: and this is deliberately NOT cleared when the set is rebuilt. `reload` runs
#: every 30s, so clearing there would empty the memo on every cycle and leave
#: the per-token parse this exists to remove. Growth is one entry per key the
#: process has ever signed with, i.e. bounded by rotations, and `_reset_for_tests`
#: clears it for isolation.
_prepared_keys: dict[str, object] = {}


def generate_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_RSA_KEY_SIZE)


def serialize_private_key(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def load_private_key(pem: str) -> rsa.RSAPrivateKey:
    """Load a PKCS8/PKCS1 PEM, refusing anything we would not sign a trust root with.

    Three refusals, all of which an operator meets at startup rather than as a
    JWKS the clouds silently cannot use, or -- worse -- one they happily can:

    * **Not RSA.** EdDSA and EC are not accepted by every cloud's workload
      identity federation, so Terrapod does not offer them.
    * **Under 2048 bits.** A 1024-bit RSA key is not considered safe for a signing
      key with a published trust root, and nothing downstream would complain: the
      JWKS is well-formed, every cloud verifies happily, and the weakness is
      invisible until someone factors it. 2048 is the floor NIST sets and what
      `generate_private_key` produces, so this refuses only a key an operator
      supplied.
    * **Exponent other than 65537.** Small exponents (3, 17) have a history of
      padding-oracle and Bleichenbacher-adjacent trouble with RSA signatures, and
      there is no reason for an issuer key to use one.

    The size and exponent rules apply to Terrapod's own generated keys as well --
    they satisfy both by construction, which is the point: a single door means a
    future change to `_RSA_KEY_SIZE` cannot quietly drop below the floor.
    """
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            f"OIDC issuer signing key must be RSA (RS256), got {type(key).__name__}. "
            "EdDSA and EC keys are not accepted by every cloud's workload identity "
            "federation, so Terrapod does not offer them."
        )
    if key.key_size < _RSA_KEY_SIZE:
        raise ValueError(
            f"OIDC issuer signing key must be at least {_RSA_KEY_SIZE} bits, got "
            f"{key.key_size}. This key signs tokens a cloud exchanges for real "
            "credentials, and its public half is published for anyone to fetch. "
            "Supply a stronger key in api.oidcSigningKey.existingSecret, or remove "
            "it and let Terrapod generate one."
        )
    if key.public_key().public_numbers().e != _RSA_PUBLIC_EXPONENT:
        raise ValueError(
            f"OIDC issuer signing key must use public exponent {_RSA_PUBLIC_EXPONENT}, "
            f"got {key.public_key().public_numbers().e}. Small exponents have a history "
            "of trouble with RSA signature padding and no issuer key needs one."
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


def _configured_next_key_pem() -> str | None:
    """The operator's INCOMING key, or None. Published, never signed with.

    Only meaningful alongside `signing_key_pem`: there is nothing to stage
    against otherwise, and reading it alone would publish a trust root nothing
    ever signs with -- strictly worse than publishing none.
    """
    from terrapod.config import settings

    if _configured_key_pem() is None:
        return None
    pem = (getattr(settings.auth.oidc_issuer, "signing_key_pem_next", "") or "").strip()
    return pem or None


def _configured_key_pem() -> str | None:
    """The operator's own key, or None.

    Read on every startup and never written anywhere, which is the whole point:
    rotating a BYO key is something the operator does to their own secret, and
    storing a copy would make the first value supplied win for ever.
    """
    from terrapod.config import settings

    pem = (getattr(settings.auth.oidc_issuer, "signing_key_pem", "") or "").strip()
    return pem or None


def validate_configured_signing_key() -> None:
    """Raise if the operator supplied a key we cannot sign with. No-op otherwise.

    Exists so the lifespan can refuse to START, which `init_oidc_signing` cannot
    do for it: that call is wrapped in a tolerant `except Exception` on purpose,
    because a pod starting before the migration Job finishes must still come up
    and the refresh task recovers it thirty seconds later.

    That tolerance is right for a database and wrong for a PEM. The key comes
    from a Secret, so nothing about it is transient and no retry can fix it --
    without this the pod reports Ready for ever while both issuer routes answer
    503 and the only evidence is one warning in the startup log. Separating the
    two is possible precisely because the key's source is not the database.

    It is NOT a config validator, though that is where it belongs on first
    reading. `terrapod.config` must import nothing from Terrapod (#1151): its
    validators run during its own module import, and the config-contract check
    parses the real models in a container holding pydantic and pyyaml alone --
    reaching `cryptography` from there is a `ModuleNotFoundError`.

    Must be called OUTSIDE the lifespan's try, or it is simply another thing the
    tolerant handler swallows. `test_the_supplied_key_check_is_outside_the_try`
    pins the position, because no behavioural test can see it.
    """
    current = _configured_key_pem()
    upcoming = _configured_next_key_pem()
    _check_one_supplied_key("auth.oidc_issuer.signing_key_pem", current)
    _check_one_supplied_key("auth.oidc_issuer.signing_key_pem_next", upcoming)

    # Distinctness, checked HERE rather than where the two are loaded, so it
    # fails startup for the same reason the rest of this does. A kid is a
    # thumbprint OF the key material, so the same key twice would publish one
    # JWK twice under one kid -- refused rather than de-duplicated, because it
    # means the operator believes they have staged a key change and have not.
    if current is not None and upcoming is not None:
        if compute_kid(load_private_key(current)) == compute_kid(load_private_key(upcoming)):
            raise ValueError(
                "auth.oidc_issuer.signing_key_pem_next is the same key as "
                "signing_key_pem, so nothing is staged. Supply the incoming key, or "
                "unset it."
            )


def _check_one_supplied_key(setting: str, configured: str | None) -> None:
    """One supplied key, or a no-op. Raises naming the setting it came from."""
    if configured is None:
        return
    try:
        load_private_key(configured)
    except Exception as exc:
        # One branch, deliberately. `load_private_key` raises ValueError for the
        # rules it enforces and cryptography raises its own for a malformed or
        # encrypted PEM; classifying them would only risk re-raising one bare,
        # without saying which Secret to go and fix.
        raise ValueError(
            f"{setting} is not usable: {exc} (it comes from the Secret named by "
            f"api.oidcSigningKey.existingSecret / nextExistingSecret)"
        ) from exc


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

        # An optional second supplied key, published and never signed with, so a
        # BYO deployment can stage a change the way a generated rotation does.
        # `_signing_kid` is set above and not touched here: the incoming key must
        # not sign, because the whole point is that the clouds have not fetched
        # it yet.
        #
        # Distinctness is enforced by `validate_configured_signing_key`, which
        # the lifespan runs outside its tolerant try -- so by the time this
        # executes the two keys are known to differ. Not re-checked here, because
        # this call site IS inside that try and a raise would only become a
        # warning.
        upcoming = _configured_next_key_pem()
        if upcoming is not None:
            next_kid = compute_kid(load_private_key(upcoming))
            _keys = [*_keys, SigningKey(kid=next_kid, private_key_pem=upcoming)]
            logger.info(
                "OIDC issuer publishing a second operator-supplied key, not signing with it",
                signing_kid=kid,
                published_kid=next_kid,
            )
        logger.info("OIDC issuer using the operator-supplied signing key", kid=kid)
        return _keys

    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _OIDC_KEY_WRITE_ADVISORY_LOCK})

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
    # Raise BEFORE assigning, exactly as `reload_signing_keys` does, and for a
    # sharper reason here. `_choose_signing_kid` raises on an empty `live`, and
    # the lifespan catches that and only WARNS — so assigning first would leave
    # `_keys == []` rather than None. `get_jwks` guards on `is None`, so the
    # empty list sails through it and the issuer publishes `{"keys": []}` with a
    # 300s cache: every federation target caches an empty trust root, and a
    # later successful rotation does not take effect until those caches expire.
    # Failing here keeps `_keys` None, which `get_jwks` refuses loudly.
    if not live:
        raise RuntimeError(
            "No live OIDC issuer signing key — every row is retired beyond "
            "retired_key_grace_seconds. Rotate to mint a new one."
        )

    _keys = [
        SigningKey(
            kid=r.kid, private_key_pem=r.private_key_pem, row_id=r.id, retired_at=r.retired_at
        )
        for r in live
    ]
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
    """The key to sign with. Raises when nothing usable is loaded.

    Checked against `_live_keys` rather than `_keys`, so a replica that has not
    been able to reload since a rotation stops signing once the chosen key ages
    out of its grace window instead of minting tokens with a key the operator
    revoked. Failing the mint is the right direction: the run fails rather than
    presenting an identity the deployment has withdrawn, and the refresh task
    recovers the process as soon as the database is reachable.
    """
    if _keys is None or _signing_kid is None:
        raise RuntimeError(
            "OIDC issuer signing key not initialised — init_oidc_signing() runs in "
            "the app lifespan."
        )
    for k in _live_keys():
        if k.kid == _signing_kid:
            return k
    raise RuntimeError(
        f"OIDC signing key {_signing_kid} is not in this process's live set — it is "
        "either absent or retired beyond retired_key_grace_seconds. This process "
        "cannot sign until it reloads from the database."
    )


async def refresh_signing_keys_cycle() -> None:
    """Periodic task: keep this process's signing set current, and recover it.

    Two jobs, and the first is why this is not simply a reload.

    `init_oidc_signing` is the only path that GENERATES a key; `reload_signing_keys`
    only re-reads. So when the lifespan's `init` failed -- a transient database
    error, or the ordinary race where the pod starts before the migration Job
    finishes -- nothing else would ever make it succeed. `reload` raises on an
    empty table for ever, `_keys` stays None, and the issuer serves 503 until
    somebody restarts the pod, on a deployment whose pod reports Ready.

    Calling `init` here is safe to repeat: it returns early for an
    operator-supplied key, and otherwise serialises on the advisory lock so
    concurrent replicas queue rather than each generating a key. It is used
    only on the recovery path, so the steady state keeps `reload`'s lighter
    read and its deliberate do-not-assign-on-failure behaviour, which leaves a
    working cache intact through a database blip.
    """
    from terrapod.db.session import get_db_session

    async with get_db_session() as db:
        if signing_keys_loaded():
            await reload_signing_keys(db)
        else:
            await init_oidc_signing(db)


def _live_keys() -> list[SigningKey]:
    """The loaded keys that are still published, judged NOW.

    `reload_signing_keys` filters the grace window when it reads, which is
    enough only while it keeps succeeding. It raises before assigning -- so a
    database outage deliberately leaves the last good set in place -- and that
    set keeps whatever retired keys it held, past their grace window, for as
    long as the outage lasts. The replica goes on publishing them in its JWKS
    and `_choose_signing_kid` can go on picking one to sign with.

    During an ordinary blip that is the right trade. During a key-compromise
    incident it is the failure the operator is specifically trying to prevent:
    they rotate, set the grace to 0 to stop publishing the compromised key now,
    and one unreachable replica keeps serving it as a trust root with no
    expiry. Filtering on read bounds that by the grace window instead of by the
    outage, which is the one thing the operator can reason about.

    Returns [] rather than raising; the callers differ in what they do about it.
    """
    if _keys is None:
        return []
    from terrapod.config import settings

    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.auth.oidc_issuer.retired_key_grace_seconds
    )
    return [k for k in _keys if k.retired_at is None or k.retired_at > cutoff]


def signing_keys_loaded() -> bool:
    """Whether a usable signing set is in this process.

    Exists so the refresh task can tell recovery from steady state without
    reaching into the module global. False means initialisation has not
    succeeded in this process -- not that the deployment has no key, which is
    the distinction the task turns on.

    It also goes False when everything loaded has aged past its grace window,
    which is what routes that recovery case into `init_oidc_signing` rather
    than `reload_signing_keys`: `reload` would raise and leave the expired set
    in place, while `init` re-reads and can mint. Both issuer documents then
    answer 503 together, which is the existing intent -- a 200 discovery
    document pointing at a JWKS that refuses is worse than neither.
    """
    return bool(_live_keys())


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

    # `_live_keys`, not `_keys`: a key past its grace window is not published,
    # and this is the document that decides what the clouds trust. The memo is
    # keyed on the LIVE kids for the same reason -- the same `_keys` yields a
    # different document once one of them ages out, so keying on `_keys` would
    # serve a revoked key from cache for as long as the process ran.
    live = _live_keys()
    if not live:
        raise RuntimeError(
            "No live OIDC issuer signing key in this process — every loaded key is "
            "retired beyond retired_key_grace_seconds. Refusing to publish an empty "
            "key set, which a cloud would cache as a trust root of nothing."
        )

    kids = tuple(k.kid for k in live)
    if _jwks_cache is not None and _jwks_cache[0] == kids:
        return _jwks_cache[1]

    jwks = {"keys": [public_jwk(load_private_key(k.private_key_pem), k.kid) for k in live]}
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

    # Serialise against init and against another replica's rotation. Held to
    # commit, so the read of what to retire and the write that retires it cannot
    # interleave with a second rotation reading the same rows.
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _OIDC_KEY_WRITE_ADVISORY_LOCK})

    # RSA-2048 keygen is hundreds of milliseconds and highly variable, and this
    # runs inside an `async def` route handler -- rule 13. Admin-only so the
    # frequency is low, but it is exactly the CPU work that rule names.
    key = await asyncio.to_thread(generate_private_key)
    kid = compute_kid(key)
    row = OIDCSigningKey(
        kid=kid,
        private_key_pem=serialize_private_key(key),
        activates_at=now + timedelta(seconds=cfg.key_propagation_seconds),
    )
    db.add(row)

    # Retire EVERY unretired key, not just the one currently signing.
    #
    # Retiring only the signer orphaned a key on the second rotation inside a
    # propagation window, which is exactly when an operator rotates twice: during
    # that window the signer is the PREVIOUS key, already retired (tier 2 of
    # `_choose_signing_kid`), so `retired_at is None` was false and the first
    # rotation's pending key was never retired at all. It stayed published, with
    # no `retired_at` for any grace window to measure, until it eventually began
    # signing and some later rotation happened to catch it. On a compromise the
    # operator believes they have revoked a key and it is still a live trust root.
    #
    # Retiring all of them is also the honest reading of what a rotation means:
    # from here on, exactly one key is current. The new row is excluded by `kid`
    # rather than by id -- it has none until flush -- and the grace window keeps
    # every retired key verifying for tokens already minted.
    unretired = (
        (
            await db.execute(
                select(OIDCSigningKey).where(
                    OIDCSigningKey.retired_at.is_(None), OIDCSigningKey.kid != kid
                )
            )
        )
        .scalars()
        .all()
    )
    for existing in unretired:
        existing.retired_at = now

    await db.commit()
    await reload_signing_keys(db)
    logger.info(
        "Rotated the OIDC issuer signing key",
        new_kid=kid,
        retired_kids=[r.kid for r in unretired],
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

    _keys = [
        SigningKey(
            kid=r.kid, private_key_pem=r.private_key_pem, row_id=r.id, retired_at=r.retired_at
        )
        for r in live
    ]
    _signing_kid = _choose_signing_kid(
        live, grace_seconds=settings.auth.oidc_issuer.retired_key_grace_seconds
    )
    return _keys


def _reset_for_tests() -> None:
    global _keys, _signing_kid, _jwks_cache, _prepared_keys  # noqa: PLW0603
    _keys = None
    _signing_kid = None
    _jwks_cache = None
    _prepared_keys = {}


def sign_identity_token(claims: dict, *, ttl_seconds: int) -> str:
    """Sign run identity claims as an RS256 JWT.

    `iat`/`exp`/`jti` are set here rather than by the caller: a caller that
    forgot one would mint a token with no expiry, and `jti` is what makes two
    tokens for the same run distinguishable in a cloud audit log.
    """
    import jwt

    global _prepared_keys  # noqa: PLW0603

    key = get_signing_key()
    now = int(time.time())
    payload = {
        **claims,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    prepared = _prepared_keys.get(key.kid)
    if prepared is None:
        prepared = load_private_key(key.private_key_pem)
        _prepared_keys[key.kid] = prepared

    return jwt.encode(payload, prepared, algorithm="RS256", headers={"kid": key.kid})
