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


class TestDualTrustAcrossARotation:
    """#2602 (c): a rotation must not take the fleet offline.

    The property is a RELATIONSHIP between two CAs -- the new one signs while the old
    one is still accepted -- so every fixture here instantiates BOTH. A fixture with
    one CA cannot tell a working trust set from a single-CA path, which is the whole
    question.
    """

    @pytest.fixture(autouse=True)
    def _isolated(self):
        """Own the module trust set for the duration, and put it back.

        The session conftest installs one CA for capability signing; these tests
        replace the whole list, so they restore it rather than appending.
        """
        from terrapod.auth import ca as ca_module

        before = list(ca_module._cas)
        yield
        ca_module._cas = before

    @staticmethod
    def _install(*cas):
        from terrapod.auth import ca as ca_module

        ca_module._cas = list(cas)

    def test_a_certificate_from_the_PREVIOUS_ca_still_verifies(self):
        """The failure this exists to prevent: rotate, and every listener in the fleet
        is refused until it happens to re-join."""
        from terrapod.auth.ca import trusted_cas

        old = CertificateAuthority.generate()
        old_cert, _ = old.issue_listener_certificate("listener-1", "pool-1")
        new = CertificateAuthority.generate()
        self._install(new, old)

        cas = trusted_cas()
        assert len(cas) == 2, "the previous CA is not trusted, so a rotation is an outage"
        assert any(self._verifies(ca, old_cert) for ca in cas), (
            "a certificate the previous CA issued is refused"
        )

    def test_the_NEW_ca_is_the_one_that_signs(self):
        old = CertificateAuthority.generate()
        new = CertificateAuthority.generate()
        self._install(new, old)
        from terrapod.auth.ca import get_ca, trusted_cas

        assert get_ca() is new
        assert trusted_cas()[0] is new, "the signer must be first; callers issue with it"

    def test_the_window_CLOSES_so_a_rotation_is_not_permanent_trust(self):
        """Trusting every CA a deployment ever had would make rotation pointless -- a
        compromised key would go on being accepted for ever, which is the thing a
        rotation exists to stop.

        The window is derived from the signer's `not_valid_before`, so an OLD signer
        means the window has already closed. No clock mocking needed.
        """
        import datetime as _dt
        from unittest.mock import patch

        from terrapod.auth.ca import trusted_cas

        old = CertificateAuthority.generate()
        new = CertificateAuthority.generate()
        self._install(new, old)

        # A signer created longer ago than one certificate lifetime.
        far_past = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=2)
        with patch.object(
            type(new.ca_cert), "not_valid_before_utc", property(lambda _self: far_past)
        ):
            cas = trusted_cas()
        assert len(cas) == 1, "the window never closes, so a rotated-away CA is trusted for ever"
        assert cas[0] is new

    def test_an_unreadable_ttl_narrows_rather_than_raises(self):
        """This runs on an authentication path, so it must not raise -- and when it
        cannot tell how wide the window is, the safe answer is the signer alone."""
        from unittest.mock import patch

        from terrapod.auth.ca import trusted_cas

        self._install(CertificateAuthority.generate(), CertificateAuthority.generate())
        with patch("terrapod.config.settings") as st:
            st.agent_pools.listener_cert_ttl_seconds = "not a number"
            cas = trusted_cas()
        assert len(cas) == 1

    def test_a_CROSS_ALGORITHM_rotation_works(self):
        """The case the whole feature exists for, and the one a naive loop breaks.

        An Ed25519 CA verifying an ML-DSA-signed certificate raises
        `InvalidSignature`; the reverse can raise something else entirely. So a
        verification loop must treat ANY failure as "not this CA" and try the next --
        treating only `InvalidSignature` that way would refuse every certificate
        issued before the rotation.
        """
        from terrapod.auth.ca import trusted_cas

        old = CertificateAuthority.generate(algorithm="ed25519")
        old_cert, _ = old.issue_listener_certificate("listener-1", "pool-1")
        new = CertificateAuthority.generate(algorithm="mldsa65")
        new_cert, _ = new.issue_listener_certificate("listener-2", "pool-1")
        self._install(new, old)

        cas = trusted_cas()
        assert any(self._verifies(ca, old_cert) for ca in cas), "the pre-rotation cert is refused"
        assert any(self._verifies(ca, new_cert) for ca in cas), "the post-rotation cert is refused"

    def test_a_certificate_from_a_CA_OUTSIDE_the_set_is_refused(self):
        """The trust set must be a set, not an invitation. Without this the tests
        above would pass over a verifier that accepted anything."""
        from terrapod.auth.ca import trusted_cas

        stranger = CertificateAuthority.generate()
        stranger_cert, _ = stranger.issue_listener_certificate("listener-x", "pool-1")
        self._install(CertificateAuthority.generate(), CertificateAuthority.generate())

        assert not any(self._verifies(ca, stranger_cert) for ca in trusted_cas())

    @staticmethod
    def _verifies(ca, cert) -> bool:
        try:
            ca.ca_cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
            return True
        except Exception:
            return False
