"""Tests for Certificate Authority — Ed25519 CA generation, listener cert issuance, serialization."""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ed25519

from terrapod.auth.ca import (
    CertificateAuthority,
    get_certificate_fingerprint,
    load_certificate,
    parse_san_uris,
    serialize_certificate,
    serialize_private_key,
)


class TestCertificateAuthorityGenerate:
    def test_generates_ed25519_keypair(self):
        ca = CertificateAuthority.generate()
        assert isinstance(ca.ca_key, ed25519.Ed25519PrivateKey)

    def test_ca_cert_is_ca(self):
        ca = CertificateAuthority.generate()
        bc = ca.ca_cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is True
        assert bc.value.path_length == 0

    def test_default_common_name(self):
        ca = CertificateAuthority.generate()
        cn = ca.ca_cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert cn[0].value == "Terrapod Certificate Authority"

    def test_custom_common_name(self):
        ca = CertificateAuthority.generate(common_name="Test CA")
        cn = ca.ca_cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert cn[0].value == "Test CA"

    def test_validity_period(self):
        ca = CertificateAuthority.generate(validity_days=365)
        now = datetime.datetime.now(datetime.UTC)
        # Should be valid now
        assert ca.ca_cert.not_valid_before_utc <= now
        # Should expire within ~366 days (allow 1 day margin)
        assert ca.ca_cert.not_valid_after_utc <= now + datetime.timedelta(days=366)
        assert ca.ca_cert.not_valid_after_utc >= now + datetime.timedelta(days=364)

    def test_self_signed(self):
        ca = CertificateAuthority.generate()
        assert ca.ca_cert.subject == ca.ca_cert.issuer

    def test_key_usage(self):
        ca = CertificateAuthority.generate()
        ku = ca.ca_cert.extensions.get_extension_for_class(x509.KeyUsage)
        assert ku.value.digital_signature is True
        assert ku.value.key_cert_sign is True
        assert ku.value.crl_sign is True
        assert ku.critical is True

    def test_ca_cert_pem_property(self):
        ca = CertificateAuthority.generate()
        pem = ca.ca_cert_pem
        assert pem.startswith("-----BEGIN CERTIFICATE-----")
        assert pem.strip().endswith("-----END CERTIFICATE-----")


class TestCertificateAuthorityLoad:
    def test_roundtrip_load(self):
        original = CertificateAuthority.generate()
        cert_pem = serialize_certificate(original.ca_cert)
        key_pem = serialize_private_key(original.ca_key)

        loaded = CertificateAuthority.load(cert_pem, key_pem)
        assert get_certificate_fingerprint(loaded.ca_cert) == get_certificate_fingerprint(
            original.ca_cert
        )

    def test_rejects_non_ed25519_key(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca = CertificateAuthority.generate()
        cert_pem = serialize_certificate(ca.ca_cert)
        rsa_pem = rsa_key.private_bytes(
            encoding=__import__(
                "cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]
            ).Encoding.PEM,
            format=__import__(
                "cryptography.hazmat.primitives.serialization", fromlist=["PrivateFormat"]
            ).PrivateFormat.PKCS8,
            encryption_algorithm=__import__(
                "cryptography.hazmat.primitives.serialization", fromlist=["NoEncryption"]
            ).NoEncryption(),
        )
        with pytest.raises(TypeError, match="Ed25519"):
            CertificateAuthority.load(cert_pem, rsa_pem)


class TestIssueListenerCertificate:
    def test_issues_valid_certificate(self):
        ca = CertificateAuthority.generate()
        cert, key = ca.issue_listener_certificate("my-listener", "default-pool")

        assert isinstance(cert, x509.Certificate)
        assert isinstance(key, ed25519.Ed25519PrivateKey)

    def test_listener_cert_is_not_ca(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("listener-1", "pool-1")
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.value.ca is False

    def test_common_name_matches_listener(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("my-listener", "my-pool")
        cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert cn[0].value == "my-listener"

    def test_issuer_is_ca(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("listener-1", "pool-1")
        assert cert.issuer == ca.ca_cert.subject

    def test_san_uris(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("my-listener", "my-pool")
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        uris = san.value.get_values_for_type(x509.UniformResourceIdentifier)
        assert "terrapod://listener/my-listener" in uris
        assert "terrapod://pool/my-pool" in uris

    def test_client_auth_eku(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("listener-1", "pool-1")
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH in eku.value

    def test_custom_ttl(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("listener-1", "pool-1", ttl_hours=24)
        now = datetime.datetime.now(datetime.UTC)
        # Should expire within ~25 hours
        assert cert.not_valid_after_utc <= now + datetime.timedelta(hours=25)
        assert cert.not_valid_after_utc >= now + datetime.timedelta(hours=23)

    def test_unique_keys_per_listener(self):
        ca = CertificateAuthority.generate()
        _, key1 = ca.issue_listener_certificate("listener-1", "pool-1")
        _, key2 = ca.issue_listener_certificate("listener-2", "pool-1")
        pub1 = key1.public_key().public_bytes_raw()
        pub2 = key2.public_key().public_bytes_raw()
        assert pub1 != pub2


class TestSerialization:
    def test_certificate_roundtrip(self):
        ca = CertificateAuthority.generate()
        pem = serialize_certificate(ca.ca_cert)
        loaded = load_certificate(pem)
        assert get_certificate_fingerprint(loaded) == get_certificate_fingerprint(ca.ca_cert)

    def test_private_key_roundtrip(self):
        ca = CertificateAuthority.generate()
        pem = serialize_private_key(ca.ca_key)
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        loaded = load_pem_private_key(pem, password=None)
        assert isinstance(loaded, ed25519.Ed25519PrivateKey)

    def test_encrypted_private_key(self):
        ca = CertificateAuthority.generate()
        pem = serialize_private_key(ca.ca_key, password=b"test-password")
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        loaded = load_pem_private_key(pem, password=b"test-password")
        assert isinstance(loaded, ed25519.Ed25519PrivateKey)

    def test_fingerprint_is_hex(self):
        ca = CertificateAuthority.generate()
        fp = get_certificate_fingerprint(ca.ca_cert)
        assert len(fp) == 64  # SHA-256 hex
        int(fp, 16)  # Valid hex


class TestParseSanUris:
    def test_extracts_listener_and_pool(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("my-listener", "my-pool")
        uris = parse_san_uris(cert)
        assert uris == {"listener": "my-listener", "pool": "my-pool"}

    def test_empty_for_ca_cert(self):
        ca = CertificateAuthority.generate()
        uris = parse_san_uris(ca.ca_cert)
        assert uris == {}
