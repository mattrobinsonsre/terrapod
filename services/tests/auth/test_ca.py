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

    def test_rejects_an_rsa_key(self):
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
        # RSA is refused for a reason beyond "untested": the capability-URL signing
        # key is derived from the CA key's RAW private bytes, which RSA does not
        # expose. So the refusal names the permitted set and says why, and the test
        # asserts the reason rather than one algorithm's name -- the message changed
        # when ML-DSA became selectable, and a test matching "Ed25519" would have
        # read as a regression rather than as the widening it was.
        with pytest.raises(TypeError, match="raw private bytes"):
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

    def test_default_ttl_is_one_hour(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("listener-1", "pool-1")
        now = datetime.datetime.now(datetime.UTC)
        # Default 3600s (1h)
        assert cert.not_valid_after_utc <= now + datetime.timedelta(seconds=3610)
        assert cert.not_valid_after_utc >= now + datetime.timedelta(seconds=3590)

    def test_custom_ttl_seconds(self):
        ca = CertificateAuthority.generate()
        cert, _ = ca.issue_listener_certificate("listener-1", "pool-1", ttl_seconds=300)
        now = datetime.datetime.now(datetime.UTC)
        # 300s (5min) — the value used in local Tilt for fast renewal cycles
        assert cert.not_valid_after_utc <= now + datetime.timedelta(seconds=310)
        assert cert.not_valid_after_utc >= now + datetime.timedelta(seconds=290)

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


class TestASelectableAlgorithm:
    """#2602: the CA may be ML-DSA, and the default stays Ed25519.

    Each case drives the whole chain -- generate, issue, verify -- rather than
    asserting a type, because what matters is that a post-quantum CA produces a
    certificate this deployment can actually authenticate with.
    """

    @pytest.mark.parametrize("algorithm", ["ed25519", "mldsa44", "mldsa65", "mldsa87"])
    def test_a_ca_of_each_algorithm_issues_a_verifiable_certificate(self, algorithm):
        from terrapod.auth.ca import _ALGORITHMS

        ca = CertificateAuthority.generate(algorithm=algorithm)
        cert, key = ca.issue_listener_certificate("listener-1", "pool-1")

        # The CA is the algorithm ASKED FOR. Without this the test passes when the
        # argument is ignored entirely and everything falls back to Ed25519 -- the
        # leaf-matches-CA assertion below stays true when BOTH are wrong, which a
        # mutation ignoring the argument proved.
        assert isinstance(ca.ca_key, _ALGORITHMS[algorithm]), (
            f"asked for {algorithm}, got {type(ca.ca_key).__name__}"
        )

        # The verification the auth path performs, in its exact two-argument form --
        # which is identical for every permitted type, and is why the algorithm is
        # selectable without touching any verifier.
        ca.ca_cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes)

        # And the leaf follows the CA rather than defaulting back to Ed25519: the
        # signature a listener proves possession with is the one that matters.
        assert type(key) is type(ca.ca_key), (
            f"a {algorithm} CA issued a {type(key).__name__} leaf, so proof of "
            "possession would still be classical"
        )

    def test_the_default_is_unchanged(self):
        """An existing deployment generating a CA must not silently move algorithm."""
        from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed

        ca = CertificateAuthority.generate()
        assert isinstance(ca.ca_key, _ed.Ed25519PrivateKey)

    @pytest.mark.parametrize("algorithm", ["mldsa44", "mldsa65", "mldsa87"])
    def test_a_post_quantum_ca_round_trips_through_storage(self, algorithm):
        """The CA is persisted as PKCS#8 PEM in a text column and loaded on every
        startup, so a key that cannot survive that is not selectable in practice.

        This also pins why no schema change was needed: the stored PEM is
        self-describing, so there is no algorithm column to add and nothing to
        migrate.
        """
        ca = CertificateAuthority.generate(algorithm=algorithm)
        loaded = CertificateAuthority.load(
            serialize_certificate(ca.ca_cert), serialize_private_key(ca.ca_key)
        )
        assert type(loaded.ca_key) is type(ca.ca_key)
        cert, _ = loaded.issue_listener_certificate("listener-2", "pool-1")
        loaded.ca_cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes)

    @pytest.mark.parametrize("algorithm", ["ed25519", "mldsa44", "mldsa65", "mldsa87"])
    def test_the_capability_url_derivation_works_for_every_permitted_type(self, algorithm):
        """The reason RSA and ECDSA are refused rather than merely untested.

        `capability_urls._signing_key()` derives from the CA key's RAW private bytes,
        an encoding only Edwards and ML-DSA keys expose. If a permitted algorithm
        could not do this, selecting it would break capability URLs -- a path with
        nothing to do with listener certificates -- so the permitted set and this
        derivation have to agree.
        """
        from cryptography.hazmat.primitives import serialization

        ca = CertificateAuthority.generate(algorithm=algorithm)
        raw = ca.ca_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        assert len(raw) >= 32

    def test_an_unknown_algorithm_is_refused_naming_the_valid_ones(self):
        with pytest.raises(ValueError, match="unknown CA algorithm"):
            CertificateAuthority.generate(algorithm="dilithium3")

    def test_an_existing_ca_keeps_its_own_algorithm_whatever_the_setting_says(self):
        """The algorithm is a property of the stored key, not of the configuration.

        An operator who sets `ca_algorithm` on a running deployment must keep signing
        with the CA they have until it is rotated -- otherwise the setting would
        silently invalidate every listener certificate in the fleet on the next
        restart.
        """

        from terrapod.config import settings

        pq = CertificateAuthority.generate(algorithm="mldsa65")
        stored_cert = serialize_certificate(pq.ca_cert)
        stored_key = serialize_private_key(pq.ca_key)

        old = settings.agent_pools.ca_algorithm
        settings.agent_pools.ca_algorithm = "ed25519"
        try:
            loaded = CertificateAuthority.load(stored_cert, stored_key)
            assert type(loaded.ca_key) is type(pq.ca_key)
            # and its leaves follow the stored key, not the setting
            _, leaf_key = loaded.issue_listener_certificate("listener-3", "pool-1")
            assert type(leaf_key) is type(pq.ca_key)
        finally:
            settings.agent_pools.ca_algorithm = old
