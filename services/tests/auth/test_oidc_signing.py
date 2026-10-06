"""The OIDC issuer's signing key: kid derivation, JWK shape, signing, BYO (#1901).

Which key signs is covered here, because it is pure arithmetic over rows and the
rotation defect it exists to prevent is invisible unless you evaluate it. The
rest of the database-backed half — init under the advisory lock, a real rotation
writing real rows — is in `tests/integration/test_oidc_signing_lifecycle.py`.
"""

import base64
import hashlib
import json
from unittest.mock import MagicMock, patch

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
        # Verified, not `verify_signature: False`. The published key is right
        # here, so skipping verification buys nothing and asserts less -- this
        # way the test also proves the token it is reading is one a cloud would
        # accept.
        pub = jwt.PyJWK(oidc_signing.get_jwks()["keys"][0]).key
        token = oidc_signing.sign_identity_token({"sub": "x", "aud": ["a"]}, ttl_seconds=60)
        claims = jwt.decode(token, pub, algorithms=["RS256"], audience="a")
        assert claims["exp"] - claims["iat"] == 60
        assert claims["nbf"] == claims["iat"]
        assert claims["jti"]

        second = oidc_signing.sign_identity_token({"sub": "x", "aud": ["a"]}, ttl_seconds=60)
        second_claims = jwt.decode(second, pub, algorithms=["RS256"], audience="a")
        assert second_claims["jti"] != claims["jti"]

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

            # Records rather than raises. Raising from `__getattr__` is both
            # flagged (`py/unexpected-raise-in-special-method`, which expects
            # AttributeError) and weaker: an AssertionError thrown here could be
            # swallowed by a `try`/`except Exception` in the code under test,
            # and the test would then pass while the database HAD been touched.
            touched: list[str] = []

            class _DBThatMustNotBeTouched:
                def __getattr__(self, name):
                    touched.append(name)
                    return MagicMock()

            keys = asyncio.run(oidc_signing.init_oidc_signing(_DBThatMustNotBeTouched()))

        assert not touched, (
            "a BYO deployment must not touch the database on this path, but "
            f"these attributes were used: {touched}"
        )

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


class TestThePublishedJWKCarriesNothingPrivate:
    """The JWKS is served anonymously to anyone who can reach the deployment, so
    a leak here is the signing key itself. `public_jwk` is an explicit
    allow-list rather than a filter over the key's own members, and this pins
    the exact set — a filter is the shape that silently starts passing a new
    member through when a library adds one.
    """

    def test_the_key_set_is_exactly_the_six_published_members(self):
        from terrapod.auth.oidc_signing import compute_kid, generate_private_key, public_jwk

        key = generate_private_key()
        jwk = public_jwk(key, compute_kid(key))
        assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}

    def test_no_private_rsa_member_is_present(self):
        """`d`, `p`, `q`, `dp`, `dq`, `qi` are the private half of an RSA JWK.
        Named individually because the point is that each is absent, not that
        some filter happened to run."""
        from terrapod.auth.oidc_signing import compute_kid, generate_private_key, public_jwk

        key = generate_private_key()
        jwk = public_jwk(key, compute_kid(key))
        for private_member in ("d", "p", "q", "dp", "dq", "qi", "oth"):
            assert private_member not in jwk

    def test_no_value_contains_pem_material(self):
        from terrapod.auth.oidc_signing import compute_kid, generate_private_key, public_jwk

        key = generate_private_key()
        jwk = public_jwk(key, compute_kid(key))
        blob = "".join(jwk.values())
        assert "PRIVATE KEY" not in blob
        assert "BEGIN" not in blob

    def test_the_jwks_is_served_from_cache_without_reparsing(self, monkeypatch):
        """The two issuer documents are necessarily unauthenticated, and
        building the JWKS parses every private key PEM. Without memoisation an
        anonymous caller can make the API do RSA key parsing at whatever rate
        they like."""
        from terrapod.auth import oidc_signing

        key = oidc_signing.generate_private_key()
        kid = oidc_signing.compute_kid(key)
        pem = oidc_signing.serialize_private_key(key)

        monkeypatch.setattr(
            oidc_signing, "_keys", [oidc_signing.SigningKey(kid=kid, private_key_pem=pem)]
        )
        monkeypatch.setattr(oidc_signing, "_jwks_cache", None)

        parses = []
        real = oidc_signing.load_private_key

        def counting(pem_text):
            parses.append(1)
            return real(pem_text)

        monkeypatch.setattr(oidc_signing, "load_private_key", counting)

        first = oidc_signing.get_jwks()
        for _ in range(20):
            oidc_signing.get_jwks()
        assert len(parses) == 1, "the PEM was re-parsed on a repeat fetch"
        assert oidc_signing.get_jwks() == first

    def test_a_changed_key_set_is_not_served_from_a_stale_cache(self, monkeypatch):
        """The cache key is the tuple of kids, and a kid is derived from the key
        material — so new material cannot reuse a kid and be served stale."""
        from terrapod.auth import oidc_signing

        def loaded(key):
            return oidc_signing.SigningKey(
                kid=oidc_signing.compute_kid(key),
                private_key_pem=oidc_signing.serialize_private_key(key),
            )

        first = loaded(oidc_signing.generate_private_key())
        monkeypatch.setattr(oidc_signing, "_keys", [first])
        monkeypatch.setattr(oidc_signing, "_jwks_cache", None)
        before = oidc_signing.get_jwks()

        second = loaded(oidc_signing.generate_private_key())
        monkeypatch.setattr(oidc_signing, "_keys", [first, second])
        after = oidc_signing.get_jwks()

        assert [k["kid"] for k in before["keys"]] == [first.kid]
        assert [k["kid"] for k in after["keys"]] == [first.kid, second.kid]


class TestAnExhaustedKeyTableNeverPublishesAnEmptyTrustRoot:
    """`init_oidc_signing` must raise BEFORE assigning `_keys`.

    The ordering is the whole test. `_choose_signing_kid` raises on an empty
    `live`, and the app lifespan catches that and only WARNS — so assigning
    first leaves `_keys == []` rather than None. `get_jwks` guards on `is None`,
    so an empty list sails straight through it and the issuer serves
    `{"keys": []}` with a 300s cache. Every federation target then caches an
    empty trust root and a later successful rotation does not take effect until
    those caches expire.

    Reachable after the emergency `propagation=0 / grace=0` rotation the chart
    advertises, or on a restart following a long outage. `reload_signing_keys`
    already had this order; `init` did not, and nothing looked at it.
    """

    def _db_returning(self, rows):
        import asyncio  # noqa: F401

        scalars = MagicMock()
        scalars.all.return_value = rows
        result = MagicMock()
        result.scalars.return_value = scalars

        class _DB:
            async def execute(self, *a, **k):
                return result

            async def commit(self):
                return None

        return _DB()

    def test_init_raises_and_leaves_the_published_set_unset(self, key):
        import asyncio
        from datetime import UTC, datetime, timedelta

        long_ago = datetime.now(UTC) - timedelta(days=365)
        row = MagicMock()
        row.kid = "stale"
        row.private_key_pem = oidc_signing.serialize_private_key(key)
        row.id = 1
        row.retired_at = long_ago

        with patch.object(oidc_signing, "_configured_key_pem", return_value=None):
            with pytest.raises(RuntimeError, match="No live OIDC issuer signing key"):
                asyncio.run(oidc_signing.init_oidc_signing(self._db_returning([row])))

        # The property the ordering exists for: NOT `{"keys": []}`.
        with pytest.raises(RuntimeError, match="not initialised"):
            oidc_signing.get_jwks()
