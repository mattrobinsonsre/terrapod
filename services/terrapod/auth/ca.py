"""Certificate Authority module for Terrapod.

Handles generation of:
- CA certificate and key (Ed25519 by default; ML-DSA selectable, see `_ALGORITHMS`)
- Listener certificates (1 year default) — for runner listener identity

Handles Terrapod's runner-only certificate needs.
"""

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, mldsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

#: The key types a Terrapod CA may use, and the ONLY ones (#2602).
#:
#: Ed25519 is the default and what every deployment has today. The ML-DSA sizes are
#: the NIST post-quantum signature standard, offered because this CA is the one
#: signature path Terrapod controls at both ends -- everything else is pinned by an
#: external verifier (see docs/post-quantum.md).
#:
#: RSA and ECDSA are deliberately absent, and not merely untested. The
#: capability-URL signing key is derived from this key's RAW private bytes
#: (`auth/capability_urls.py`), an encoding only Edwards and ML-DSA keys expose, so
#: admitting one of those would break a path with nothing to do with listener
#: certificates. Keeping the set closed is what keeps that derivation sound.
_ALGORITHMS: dict[str, type] = {
    "ed25519": ed25519.Ed25519PrivateKey,
    "mldsa44": mldsa.MLDSA44PrivateKey,
    "mldsa65": mldsa.MLDSA65PrivateKey,
    "mldsa87": mldsa.MLDSA87PrivateKey,
}

#: Any key type a CA may hold. Used for the isinstance gate and the type hints, so
#: the permitted set has exactly one definition.
CAPrivateKey = (
    ed25519.Ed25519PrivateKey
    | mldsa.MLDSA44PrivateKey
    | mldsa.MLDSA65PrivateKey
    | mldsa.MLDSA87PrivateKey
)

# Module-level CA singleton, initialized in lifespan
#: Every CA this node trusts, NEWEST FIRST. `_cas[0]` is the one that signs.
#:
#: A list rather than a single slot because a rotation has to overlap (#2602): the
#: certificates the previous CA issued are still live for up to their TTL after the
#: new CA starts signing, so refusing them would take the whole fleet offline at the
#: moment of rotation and heal only as each listener re-joined.
_cas: "list[CertificateAuthority]" = []


def _generate_key(algorithm: str | None = None):
    """A new CA or leaf private key of the configured (or named) algorithm.

    One producer, so the permitted set is enforced in a single place and an unknown
    name fails here with the valid ones listed rather than somewhere downstream.
    """
    from terrapod.config import settings

    name = (algorithm or settings.agent_pools.ca_algorithm or "ed25519").strip().lower()
    key_type = _ALGORITHMS.get(name)
    if key_type is None:
        raise ValueError(
            f"unknown CA algorithm {name!r}; valid values are {', '.join(_ALGORITHMS)}"
        )
    return key_type.generate()


def _algorithm_name(key) -> str:
    """The `_ALGORITHMS` name for a key we are holding.

    Derived from the key's own type rather than from configuration, because an
    existing CA's algorithm is a property of the stored key: a deployment that
    changes the setting keeps signing with what it has until the CA is rotated, and
    the leaf must follow the CA rather than the setting.
    """
    for name, key_type in _ALGORITHMS.items():
        if isinstance(key, key_type):
            return name
    raise TypeError(f"not a permitted CA key type: {type(key).__name__}")


class CertificateAuthority:
    """Certificate Authority for issuing listener certificates."""

    def __init__(
        self,
        ca_cert: x509.Certificate,
        ca_key: CAPrivateKey,
    ):
        self._ca_cert = ca_cert
        self._ca_key = ca_key

    @property
    def ca_cert(self) -> x509.Certificate:
        return self._ca_cert

    @property
    def ca_key(self) -> CAPrivateKey:
        return self._ca_key

    @property
    def ca_cert_pem(self) -> str:
        """Return the CA certificate as a PEM string."""
        return self._ca_cert.public_bytes(serialization.Encoding.PEM).decode()

    @classmethod
    def generate(
        cls,
        common_name: str = "Terrapod Certificate Authority",
        validity_days: int = 3650,
        algorithm: str | None = None,
    ) -> "CertificateAuthority":
        """Generate a new CA certificate and key pair.

        `algorithm` is one of `_ALGORITHMS`; None reads
        `settings.agent_pools.ca_algorithm`, which defaults to `ed25519`. It is read
        here rather than at the call site so every path that creates a CA -- startup,
        a test, a future rotation -- honours the setting without having to remember to.

        A certificate is signed with `.sign(key, None)` for every permitted type:
        Ed25519 and ML-DSA both carry their hash internally, which is why the
        algorithm is selectable without touching the builder or any verifier.
        """
        private_key = _generate_key(algorithm)
        public_key = private_key.public_key()

        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Terrapod"),
            ]
        )

        now = datetime.datetime.now(datetime.UTC)

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=validity_days))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=0),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    key_encipherment=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(public_key),
                critical=False,
            )
            .sign(private_key, None)  # Ed25519 and ML-DSA both carry their own hash
        )

        logger.info(
            "Generated new CA certificate",
            common_name=common_name,
            expires=cert.not_valid_after_utc.isoformat(),
        )

        return cls(ca_cert=cert, ca_key=private_key)

    @classmethod
    def load(cls, cert_pem: bytes, key_pem: bytes) -> "CertificateAuthority":
        """Load CA from PEM-encoded certificate and key."""
        cert = x509.load_pem_x509_certificate(cert_pem)
        key = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(key, tuple(_ALGORITHMS.values())):
            raise TypeError(
                f"Expected one of {', '.join(_ALGORITHMS)}, got {type(key).__name__}. "
                "RSA and ECDSA are not supported: the capability-URL signing key is "
                "derived from this key's raw private bytes, which they do not expose."
            )
        return cls(ca_cert=cert, ca_key=key)

    def issue_listener_certificate(
        self,
        name: str,
        pool_name: str,
        ttl_seconds: int = 3600,
    ) -> tuple[x509.Certificate, CAPrivateKey]:
        """Issue a certificate for a runner listener.

        Args:
            name: Listener name (used as CN).
            pool_name: Agent pool name (embedded in SAN URI).
            ttl_seconds: Certificate lifetime in seconds (default: 1h).
                Listeners renew at 50% of this lifetime plus a per-pod
                splay; tighter values constrain how long a leaked cert
                stays usable but increase API renewal traffic.

        Returns:
            Tuple of (certificate, private_key).
        """
        # The leaf follows the CA's own algorithm. A post-quantum CA issuing Ed25519
        # leaves would leave the signature a listener actually proves possession with
        # classical, which is most of what the change was for -- and the two are
        # independently expressible, so this is a decision rather than a consequence.
        private_key = _generate_key(_algorithm_name(self._ca_key))
        public_key = private_key.public_key()

        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, name),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Terrapod"),
            ]
        )

        now = datetime.datetime.now(datetime.UTC)

        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._ca_cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(seconds=ttl_seconds))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.UniformResourceIdentifier(f"terrapod://listener/{name}"),
                        x509.UniformResourceIdentifier(f"terrapod://pool/{pool_name}"),
                    ]
                ),
                critical=False,
            )
        )

        cert = builder.sign(self._ca_key, None)

        logger.info(
            "Issued listener certificate",
            name=name,
            pool=pool_name,
            expires=cert.not_valid_after_utc.isoformat(),
        )

        return cert, private_key


# ── Serialization Helpers ────────────────────────────────────────────────


def serialize_certificate(cert: x509.Certificate) -> bytes:
    """Serialize certificate to PEM format."""
    return cert.public_bytes(serialization.Encoding.PEM)


def serialize_private_key(
    key: CAPrivateKey,
    password: bytes | None = None,
) -> bytes:
    """Serialize private key to PEM format."""
    encryption: serialization.KeySerializationEncryption
    if password:
        encryption = serialization.BestAvailableEncryption(password)
    else:
        encryption = serialization.NoEncryption()

    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )


def load_certificate(pem_data: bytes) -> x509.Certificate:
    """Load certificate from PEM data."""
    return x509.load_pem_x509_certificate(pem_data)


def get_certificate_fingerprint(cert: x509.Certificate) -> str:
    """Get SHA256 fingerprint of certificate."""
    return cert.fingerprint(hashes.SHA256()).hex()


def parse_san_uris(cert: x509.Certificate) -> dict[str, str]:
    """Extract terrapod:// SAN URIs from a certificate.

    Returns a dict like:
        {"listener": "my-listener", "pool": "default"}
    """
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return {}

    result: dict[str, str] = {}
    for uri in san.value.get_values_for_type(x509.UniformResourceIdentifier):
        if uri.startswith("terrapod://"):
            parts = uri[len("terrapod://") :].split("/", 1)
            if len(parts) == 2:
                result[parts[0]] = parts[1]
    return result


# ── Lifecycle ────────────────────────────────────────────────────────────

# Stable, arbitrary key for the Postgres advisory lock that serializes CA
# initialization across API replicas (#1060). Any fixed bigint works; it only
# needs to be unique among advisory-lock keys the app uses.
_CA_INIT_ADVISORY_LOCK = 776699001122


async def init_ca(db: AsyncSession) -> CertificateAuthority:
    """Initialize the CA singleton from database, falling back to generate + persist.

    Startup sequence:
    1. Query certificate_authority table for existing CA
    2. If found: load cert + key from PEM columns, set singleton
    3. If not found: generate new CA, persist to DB, set singleton
    4. Also write to filesystem as cache
    """
    from terrapod.db.models import CertificateAuthorityModel

    global _cas  # noqa: PLW0603

    # Serialize the check-then-create across replicas with a transaction-scoped
    # Postgres advisory lock (multi-replica safe, no leader election — same
    # mutual-exclusion philosophy as the scheduler's Redis mutex). Without this,
    # two API replicas starting against a fresh (CA-less) database each see
    # "no CA", each generate one, and each insert a row — leaving the replicas
    # caching *different* CAs and permanently disagreeing, which breaks listener
    # cert validation ("Certificate not signed by this CA"). The lock makes
    # concurrent starters queue: the first creates the CA, the rest load it. The
    # lock releases automatically when this transaction commits below (#1060).
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CA_INIT_ADVISORY_LOCK})

    # Every row, newest first -- not just the newest. The newest signs; the rest are
    # trusted for as long as anything they signed can still be valid, which
    # `trusted_cas` derives rather than storing.
    result = await db.execute(
        select(CertificateAuthorityModel).order_by(CertificateAuthorityModel.created_at.desc())
    )
    ca_records = list(result.scalars().all())
    ca_record = ca_records[0] if ca_records else None

    if ca_record:
        loaded = [
            CertificateAuthority.load(r.ca_cert.encode(), r.ca_key_pem.encode()) for r in ca_records
        ]
        ca = loaded[0]
        # Release the advisory lock (held on this transaction) now that the
        # read is done; nothing was written.
        await db.commit()
        logger.info(
            "Loaded CA from database",
            fingerprint=get_certificate_fingerprint(ca.ca_cert)[:16],
        )
    else:
        ca = CertificateAuthority.generate()

        cert_pem = serialize_certificate(ca.ca_cert).decode()
        key_pem = serialize_private_key(ca.ca_key).decode()

        ca_record = CertificateAuthorityModel(
            ca_cert=cert_pem,
            ca_key_pem=key_pem,
        )
        db.add(ca_record)
        await db.commit()

        logger.info(
            "Generated and stored new CA in database",
            fingerprint=get_certificate_fingerprint(ca.ca_cert)[:16],
        )

    # The database is the single source of truth for the CA, loaded on every
    # startup. We deliberately do NOT also write the CA *private key* to the
    # pod filesystem — that cache was never read back (init_ca always loads
    # from the DB) and writing the private key to disk is needless attack
    # surface.
    # `ca_records` and NOT `ca_record`: the generate branch REASSIGNS `ca_record` to
    # the row it just built, so testing that would read as "rows existed" on a fresh
    # database and reach an undefined `loaded`. Caught by the init-race test.
    _cas = loaded if ca_records else [ca]
    return _cas[0]


def get_ca() -> CertificateAuthority:
    """The CA that SIGNS. Raises if not initialized.

    Every caller that issues or derives wants this one, not the trust set: a new
    certificate is signed by the current CA, and the capability-URL signing key is
    derived from it. `trusted_cas` is for the verification side.
    """
    if not _cas:
        raise RuntimeError("CA not initialized — call init_ca() first")
    return _cas[0]


def trusted_cas() -> list["CertificateAuthority"]:
    """Every CA whose certificates this node still accepts, newest first.

    Exactly one entry outside a rotation window, so the common path is unchanged.

    **The window is derived, not stored.** A CA certificate's `not_valid_before` is
    the moment it was created, and nothing it signed outlives one
    `listener_cert_ttl_seconds` after a newer CA took over signing -- listeners renew
    at half that. So a predecessor is trusted until
    `signer.not_valid_before + listener_cert_ttl_seconds`, and after that only the
    signer is. No column, no retirement job, and nothing to leave stale: the window
    closes by the clock whether or not anyone remembers it.

    Deliberately NOT unbounded. Trusting every CA a deployment ever had would make
    rotation pointless -- a compromised key would go on being accepted for ever,
    which is the thing a rotation exists to stop.
    """
    from terrapod.config import settings

    # The head comes from `get_ca()` rather than `_cas[0]`, so there is exactly ONE
    # definition of "which CA signs" and anything that substitutes it -- a test, a
    # future per-request override -- is honoured by verification too. Reading
    # `_cas[0]` here instead would let the signing and verifying halves disagree
    # about the signer, which is the hardest kind of auth bug to see.
    signer = get_ca()
    others = [c for c in _cas if c is not signer]
    if not others:
        return [signer]
    # Defensive, because this is on an authentication path: an unreadable TTL must
    # not make verification RAISE. Falling back to the signer alone is the
    # conservative answer -- it narrows the trust set rather than widening it, so the
    # failure mode is a refused certificate during a rotation, not an accepted one.
    try:
        window = datetime.timedelta(seconds=int(settings.agent_pools.listener_cert_ttl_seconds))
    except (TypeError, ValueError, AttributeError):
        logger.warning(
            "Could not read listener_cert_ttl_seconds; trusting only the signing CA. "
            "A certificate issued by a previous CA will be refused until listeners renew."
        )
        return [signer]
    if datetime.datetime.now(datetime.UTC) >= signer.ca_cert.not_valid_before_utc + window:
        return [signer]
    return [signer, *others]


async def rotate_ca(db: AsyncSession, *, algorithm: str | None = None) -> "CertificateAuthority":
    """Generate a new CA, which becomes the signer, and keep trusting the old one.

    This is the rotation path, and it is an INSERT rather than a replacement: the
    read takes the newest row, so adding one is what moves the signer. The previous
    CA stays in the table and stays trusted for one `listener_cert_ttl_seconds` --
    see `trusted_cas` -- which is exactly as long as a certificate it issued can
    still be live.

    What an operator sees: nothing. Listeners renew on their own cycle and pick up a
    certificate from the new chain within half a TTL, and until then their existing
    ones keep authenticating. No re-join, no restart, no window where the fleet is
    refused.

    `algorithm` is how a deployment moves to ML-DSA: the setting is read only when a
    CA is generated, and this is the generating path.

    NOT named `issue_*`. `test_replication_node_local` asserts the CA has exactly one
    issuing method, with "reopen #1143 if it now signs anything durable" -- and this
    signs nothing, it creates a signer.
    """
    from terrapod.db.models import CertificateAuthorityModel

    global _cas  # noqa: PLW0603

    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CA_INIT_ADVISORY_LOCK})
    ca = CertificateAuthority.generate(algorithm=algorithm)
    db.add(
        CertificateAuthorityModel(
            ca_cert=serialize_certificate(ca.ca_cert).decode(),
            ca_key_pem=serialize_private_key(ca.ca_key).decode(),
        )
    )
    await db.commit()
    _cas = [ca, *_cas]
    logger.warning(
        "Rotated the listener CA. The previous one stays trusted for one certificate "
        "lifetime while listeners renew onto the new chain.",
        fingerprint=get_certificate_fingerprint(ca.ca_cert)[:16],
        trusted=len(_cas),
    )
    return ca
