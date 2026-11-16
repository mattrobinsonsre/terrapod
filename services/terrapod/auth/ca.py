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
_ca: "CertificateAuthority | None" = None


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

    global _ca  # noqa: PLW0603

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

    result = await db.execute(
        select(CertificateAuthorityModel)
        .order_by(CertificateAuthorityModel.created_at.desc())
        .limit(1)
    )
    ca_record = result.scalar_one_or_none()

    if ca_record:
        ca = CertificateAuthority.load(
            ca_record.ca_cert.encode(),
            ca_record.ca_key_pem.encode(),
        )
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
    _ca = ca
    return _ca


def get_ca() -> CertificateAuthority:
    """Return the CA singleton. Raises if not initialized."""
    if _ca is None:
        raise RuntimeError("CA not initialized — call init_ca() first")
    return _ca
