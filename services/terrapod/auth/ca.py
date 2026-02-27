"""Certificate Authority module for Terrapod.

Handles generation of:
- CA certificate and key (Ed25519)
- Listener certificates (1 year default) — for runner listener identity

Ported from bamf, simplified for Terrapod's runner-only certificate needs.
"""

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Module-level CA singleton, initialized in lifespan
_ca: "CertificateAuthority | None" = None

# Default CA data directory
CA_DATA_DIR = Path("/var/lib/terrapod/ca")


class CertificateAuthority:
    """Certificate Authority for issuing listener certificates."""

    def __init__(
        self,
        ca_cert: x509.Certificate,
        ca_key: ed25519.Ed25519PrivateKey,
    ):
        self._ca_cert = ca_cert
        self._ca_key = ca_key

    @property
    def ca_cert(self) -> x509.Certificate:
        return self._ca_cert

    @property
    def ca_key(self) -> ed25519.Ed25519PrivateKey:
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
    ) -> "CertificateAuthority":
        """Generate a new CA certificate and Ed25519 key pair."""
        private_key = ed25519.Ed25519PrivateKey.generate()
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
            .sign(private_key, None)  # Ed25519 doesn't use a hash algorithm
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
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise TypeError(f"Expected Ed25519 private key, got {type(key).__name__}")
        return cls(ca_cert=cert, ca_key=key)

    def issue_listener_certificate(
        self,
        name: str,
        pool_name: str,
        ttl_hours: int = 8760,
    ) -> tuple[x509.Certificate, ed25519.Ed25519PrivateKey]:
        """Issue a certificate for a runner listener.

        Args:
            name: Listener name (used as CN).
            pool_name: Agent pool name (embedded in SAN URI).
            ttl_hours: Certificate lifetime (default: 1 year).

        Returns:
            Tuple of (certificate, private_key).
        """
        private_key = ed25519.Ed25519PrivateKey.generate()
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
            .not_valid_after(now + datetime.timedelta(hours=ttl_hours))
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
    key: ed25519.Ed25519PrivateKey,
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
            parts = uri[len("terrapod://"):].split("/", 1)
            if len(parts) == 2:
                result[parts[0]] = parts[1]
    return result


# ── Lifecycle ────────────────────────────────────────────────────────────


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

    result = await db.execute(
        select(CertificateAuthorityModel)
        .order_by(CertificateAuthorityModel.created_at.desc())
        .limit(1)
    )
    ca_record = result.scalar_one_or_none()

    if ca_record:
        ca = CertificateAuthority.load(
            ca_record.ca_cert.encode(),
            ca_record.ca_key_encrypted.encode(),
        )
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
            ca_key_encrypted=key_pem,
        )
        db.add(ca_record)
        await db.commit()

        logger.info(
            "Generated and stored new CA in database",
            fingerprint=get_certificate_fingerprint(ca.ca_cert)[:16],
        )

    # Write to filesystem as cache
    try:
        CA_DATA_DIR.mkdir(parents=True, exist_ok=True)
        cert_path = CA_DATA_DIR / "ca.crt"
        key_path = CA_DATA_DIR / "ca.key"
        cert_path.write_bytes(serialize_certificate(ca.ca_cert))
        key_path.write_bytes(serialize_private_key(ca.ca_key))
        key_path.chmod(0o600)
    except OSError as e:
        logger.warning("Failed to write CA cache to filesystem", error=str(e))

    _ca = ca
    return _ca


def get_ca() -> CertificateAuthority:
    """Return the CA singleton. Raises if not initialized."""
    if _ca is None:
        raise RuntimeError("CA not initialized — call init_ca() first")
    return _ca
