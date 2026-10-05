"""The OIDC issuer's signing key: kid derivation, JWK shape, signing, BYO (#1901).

Which key signs is covered here, because it is pure arithmetic over rows and the
rotation defect it exists to prevent is invisible unless you evaluate it. The
rest of the database-backed half — init under the advisory lock, a real rotation
writing real rows — is in `tests/integration/test_oidc_signing_lifecycle.py`.
"""

import base64
import hashlib
import json
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from terrapod.auth import oidc_signing


@pytest.fixture(autouse=True)
def _reset():
    oidc_signing._reset_for_tests()
    yield
    oidc_signing._reset_for_tests()


@pytest.fixture
def key():
    return oidc_signing.generate_private_key()


class TestTheKid:
    """`kid` is an RFC 7638 thumbprint of the public key, not an assigned id."""

    def test_it_is_derived_from_the_key_and_so_is_stable(self, key):
        """Two calls on the same key agree, which is what lets a restart keep
        publishing the same kid without storing it."""
        assert oidc_signing.compute_kid(key) == oidc_signing.compute_kid(key)

    def test_different_keys_get_different_kids(self):
        a = oidc_signing.generate_private_key()
        b = oidc_signing.generate_private_key()
        assert oidc_signing.compute_kid(a) != oidc_signing.compute_kid(b)

    def test_a_pem_round_trip_preserves_the_kid(self, key):
        """The thumbprint has to survive serialisation, or a key loaded from the
        database would publish a different kid than the one it was stored with
        and every token already in flight would reference a kid the JWKS does
        not contain."""
        reloaded = oidc_signing.load_private_key(oidc_signing.serialize_private_key(key))
        assert oidc_signing.compute_kid(reloaded) == oidc_signing.compute_kid(key)

    def test_it_matches_an_independent_rfc7638_computation(self, key):
        """Computed here from the spec rather than from our own helper: the
        required members only (`e`, `kty`, `n`), lexicographic, no whitespace,
        SHA-256, base64url unpadded. If ours drifts from the spec a cloud
        picking a key by kid stops finding it."""
        numbers = key.public_key().public_numbers()

        def b64(i: int) -> str:
            raw = i.to_bytes((i.bit_length() + 7) // 8, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        canonical = f'{{"e":"{b64(numbers.e)}","kty":"RSA","n":"{b64(numbers.n)}"}}'  # noqa: E231
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert oidc_signing.compute_kid(key) == expected


class TestThePublishedJWK:
    def test_it_carries_what_a_verifier_needs_and_no_private_material(self, key):
        jwk = oidc_signing.public_jwk(key, "some-kid")
        assert jwk["kty"] == "RSA"
        assert jwk["alg"] == "RS256"
        assert jwk["use"] == "sig"
        assert jwk["kid"] == "some-kid"
        assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}
        # The private exponent and the primes must not be reachable from the
        # published document under any key name.
        blob = json.dumps(jwk)
        private = key.private_numbers()
        for secret in (private.d, private.p, private.q):
            raw = secret.to_bytes((secret.bit_length() + 7) // 8, "big")
            assert base64.urlsafe_b64encode(raw).rstrip(b"=").decode() not in blob


class TestLoading:
    def test_a_non_rsa_key_is_refused_by_name(self):
        """An operator supplying an Ed25519 key gets told at startup. Accepting
        it would publish a JWKS the clouds cannot use, and the failure would
        surface as every federated workspace breaking with nothing local to
        look at."""
        pem = (
            ed25519.Ed25519PrivateKey.generate()
            .private_bytes(
                encoding=__import__(
                    "cryptography.hazmat.primitives.serialization", fromlist=["x"]
                ).Encoding.PEM,
                format=__import__(
                    "cryptography.hazmat.primitives.serialization", fromlist=["x"]
                ).PrivateFormat.PKCS8,
                encryption_algorithm=__import__(
                    "cryptography.hazmat.primitives.serialization", fromlist=["x"]
                ).NoEncryption(),
            )
            .decode()
        )
        with pytest.raises(ValueError, match="must be RSA"):
            oidc_signing.load_private_key(pem)

    def test_the_generated_key_is_at_least_2048_bits(self, key):
        """Below 2048 is refused outright by the clouds."""
        assert isinstance(key, rsa.RSAPrivateKey)
        assert key.key_size >= 2048


class TestSigning:
    """A signed token has to verify against the published JWK, because that is
    exactly what the cloud will do with it."""

    def _install(self, key):
        kid = oidc_signing.compute_kid(key)
        oidc_signing._keys = [
            oidc_signing.SigningKey(
                kid=kid, private_key_pem=oidc_signing.serialize_private_key(key)
            )
        ]
        oidc_signing._signing_kid = kid
        return kid

    def test_a_signed_token_verifies_against_the_published_key(self, key):
        kid = self._install(key)
        token = oidc_signing.sign_identity_token(
            {"iss": "https://terrapod.example.com", "sub": "workspace:dns", "aud": ["a"]},
            ttl_seconds=900,
        )
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "RS256"
        assert header["kid"] == kid

        published = oidc_signing.get_jwks()["keys"][0]
        pub = jwt.PyJWK(published).key
        claims = jwt.decode(token, pub, algorithms=["RS256"], audience="a")
        assert claims["sub"] == "workspace:dns"
        assert claims["iss"] == "https://terrapod.example.com"

    def test_expiry_and_jti_are_set_here_not_by_the_caller(self, key):
        """A caller that forgot `exp` would mint a token with no expiry, and
        `jti` is what makes two tokens for one run distinguishable in a cloud
        audit log."""
        self._install(key)
        token = oidc_signing.sign_identity_token({"sub": "x", "aud": ["a"]}, ttl_seconds=60)
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["exp"] - claims["iat"] == 60
        assert claims["nbf"] == claims["iat"]
        assert claims["jti"]

        second = oidc_signing.sign_identity_token({"sub": "x", "aud": ["a"]}, ttl_seconds=60)
        assert jwt.decode(second, options={"verify_signature": False})["jti"] != claims["jti"]

    def test_signing_before_initialisation_raises_rather_than_improvising(self):
        with pytest.raises(RuntimeError, match="not initialised"):
            oidc_signing.sign_identity_token({"sub": "x"}, ttl_seconds=60)

    def test_the_jwks_refuses_before_initialisation(self):
        """A route serving an empty key set would read to a cloud as "this issuer
        has no keys", which is a worse answer than an error."""
        with pytest.raises(RuntimeError, match="not initialised"):
            oidc_signing.get_jwks()


class TestTheOperatorSuppliedKey:
    """BYO wins on every startup and is never stored — the #1994 rule, in a more
    expensive place: storing a copy would make the first value supplied win for
    ever and silently ignore every later rotation."""

    def test_a_configured_key_is_used_and_nothing_is_written(self, key):
        pem = oidc_signing.serialize_private_key(key)
        with patch.object(oidc_signing, "_configured_key_pem", return_value=pem):
            import asyncio

            class _DBThatMustNotBeTouched:
                def __getattr__(self, name):
                    raise AssertionError(
                        f"a BYO deployment must not touch the database on this path, "
                        f"but db.{name} was called"
                    )

            keys = asyncio.run(oidc_signing.init_oidc_signing(_DBThatMustNotBeTouched()))

        assert len(keys) == 1
        assert keys[0].kid == oidc_signing.compute_kid(key)
        # No row, so nothing for us to rotate.
        assert keys[0].row_id is None

    def test_rotation_is_refused_for_a_configured_key(self, key):
        import asyncio

        pem = oidc_signing.serialize_private_key(key)
        with patch.object(oidc_signing, "_configured_key_pem", return_value=pem):
            with pytest.raises(ValueError, match="operator-supplied"):
                asyncio.run(oidc_signing.rotate_signing_key(object()))


class TestWhichKeySigns:
    """`_choose_signing_kid`, and the rotation window it exists to honour.

    This class is here because the defect it pins was invisible to every other
    test: a rotation leaves tier 1 empty by construction, so getting the fallback
    order wrong made `key_propagation_seconds` dead while everything stayed
    green. It was found by evaluating the function against the rows a real
    rotation produces, which is what these tests do.
    """

    GRACE = 3600
    PROPAGATION = 600

    def _row(self, kid, *, created, activates=None, retired=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            kid=kid,
            created_at=created,
            activates_at=activates if activates is not None else created,
            retired_at=retired,
        )

    def test_the_newest_active_key_signs(self):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        older = self._row("OLDER", created=now - timedelta(days=10))
        newer = self._row("NEWER", created=now - timedelta(days=1))
        assert oidc_signing._choose_signing_kid([older, newer], grace_seconds=self.GRACE) == "NEWER"

    def test_a_rotation_keeps_signing_with_the_OLD_key_until_the_new_one_propagates(self):
        """THE regression test. A published trust root cannot be swapped
        atomically: the clouds cache the JWKS, so the incoming key is the one
        they do NOT have, and the outgoing key is still published for its grace
        window. Signing with the new key here is exactly the outage
        `key_propagation_seconds` exists to prevent."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        old = self._row("OLD", created=now - timedelta(days=30), retired=now)
        new = self._row("NEW", created=now, activates=now + timedelta(seconds=self.PROPAGATION))
        assert oidc_signing._choose_signing_kid([old, new], grace_seconds=self.GRACE) == "OLD"

    def test_once_the_window_has_passed_the_new_key_signs(self):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        old = self._row(
            "OLD", created=now - timedelta(days=30), retired=now - timedelta(seconds=700)
        )
        new = self._row(
            "NEW", created=now - timedelta(seconds=700), activates=now - timedelta(seconds=100)
        )
        assert oidc_signing._choose_signing_kid([old, new], grace_seconds=self.GRACE) == "NEW"

    def test_a_retired_key_past_its_grace_does_not_come_back(self):
        """It is no longer in the published JWKS, so signing with it would
        produce tokens nothing can verify — the failure the last-resort branch
        at least warns about, with none of the warning."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        old = self._row(
            "OLD",
            created=now - timedelta(days=30),
            retired=now - timedelta(seconds=self.GRACE + 60),
        )
        new = self._row("NEW", created=now, activates=now + timedelta(seconds=self.PROPAGATION))
        assert oidc_signing._choose_signing_kid([old, new], grace_seconds=self.GRACE) == "NEW"

    def test_everything_retired_past_grace_raises_rather_than_signing_blind(self):
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        dead = self._row(
            "DEAD",
            created=now - timedelta(days=30),
            retired=now - timedelta(seconds=self.GRACE + 60),
        )
        with pytest.raises(RuntimeError, match="past its grace"):
            oidc_signing._choose_signing_kid([dead], grace_seconds=self.GRACE)

    def test_a_single_not_yet_active_key_signs_with_a_warning(self, capsys):
        """The genuine last resort: nothing retired is still published and the
        only key has a future activation. Signing beats not signing, but the
        operator has to be told."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        only = self._row("ONLY", created=now, activates=now + timedelta(seconds=60))
        assert oidc_signing._choose_signing_kid([only], grace_seconds=self.GRACE) == "ONLY"
        # structlog writes to stdout here, not through the stdlib logging
        # capture — `caplog.text` is empty, so asserting against it would pass
        # whatever the code did.
        assert "may not hold it yet" in capsys.readouterr().out
