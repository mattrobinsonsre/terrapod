"""The listener certificate alone is a bearer credential; these pin the proof.

Every test here is about a property that, if it regressed, would leave the
certificate replayable while the code still looked like it checked something.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from terrapod.auth import listener_pop as pop


def _cert_and_key(cn: str = "listener-1"):
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, None)
    )
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert, pem


class _FakeRedis:
    """Honours `nx`, because that is the entire replay mechanism. A fake that
    ignored it would make every replay test pass while proving nothing."""

    def __init__(self):
        self.keys: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture
def redis():
    r = _FakeRedis()
    with patch("terrapod.redis.client.get_redis_client", return_value=r):
        yield r


def _signed(pem, method="GET", path="/api/v1/runs/next", nonce="n" * 24, ts=None):
    ts = str(int(time.time())) if ts is None else str(ts)
    return {
        "method": method,
        "path": path,
        "timestamp": ts,
        "nonce": nonce,
        "signature": pop.sign_request(pem, method, path, ts, nonce),
    }


class TestAProofOfPossessionIsRequired:
    async def test_a_correctly_signed_request_verifies(self, redis):
        cert, pem = _cert_and_key()
        await pop.verify_request(cert, **_signed(pem))

    async def test_a_signature_from_another_listeners_key_is_refused(self, redis):
        cert, _ = _cert_and_key("listener-1")
        _, other_pem = _cert_and_key("listener-2")
        with pytest.raises(pop.ProofOfPossessionError, match="does not match"):
            await pop.verify_request(cert, **_signed(other_pem))


class TestASignatureIsBoundToOneRequest:
    async def test_it_cannot_be_moved_to_another_path(self, redis):
        cert, pem = _cert_and_key()
        args = _signed(pem, path="/api/v1/runs/next")
        args["path"] = "/api/v1/listeners/abc/runner-token"
        with pytest.raises(pop.ProofOfPossessionError, match="does not match"):
            await pop.verify_request(cert, **args)

    async def test_it_cannot_be_moved_to_another_method(self, redis):
        cert, pem = _cert_and_key()
        args = _signed(pem, method="GET")
        args["method"] = "POST"
        with pytest.raises(pop.ProofOfPossessionError, match="does not match"):
            await pop.verify_request(cert, **args)


class TestTheWindowAndTheNonce:
    @pytest.mark.parametrize("skew", [-(pop.CLOCK_SKEW_SECONDS + 30), pop.CLOCK_SKEW_SECONDS + 30])
    async def test_a_timestamp_outside_the_window_is_refused(self, redis, skew):
        cert, pem = _cert_and_key()
        with pytest.raises(pop.ProofOfPossessionError, match="window"):
            await pop.verify_request(cert, **_signed(pem, ts=int(time.time()) + skew))

    async def test_the_same_signature_cannot_be_presented_twice(self, redis):
        cert, pem = _cert_and_key()
        args = _signed(pem)
        await pop.verify_request(cert, **args)
        with pytest.raises(pop.ProofOfPossessionError, match="already been used"):
            await pop.verify_request(cert, **args)

    async def test_a_short_nonce_is_refused(self, redis):
        cert, pem = _cert_and_key()
        with pytest.raises(pop.ProofOfPossessionError, match="nonce"):
            await pop.verify_request(cert, **_signed(pem, nonce="short"))

    async def test_a_failed_signature_does_not_spend_the_nonce(self, redis):
        """The ordering property, and the reason it matters.

        If the nonce were claimed before the signature verified, anyone who could
        reach the endpoint could present a victim's nonce with rubbish in the
        signature, burn it, and have the victim's own genuine retry rejected as a
        replay — a denial of service built out of the anti-replay measure. So a
        rejected signature must leave the nonce unspent and reusable.
        """
        cert, pem = _cert_and_key()
        _, other_pem = _cert_and_key()
        nonce = "z" * 24
        with pytest.raises(pop.ProofOfPossessionError):
            await pop.verify_request(cert, **_signed(other_pem, nonce=nonce))
        assert redis.keys == {}, "a rejected request must not spend the nonce"
        await pop.verify_request(cert, **_signed(pem, nonce=nonce))


class TestMalformedInput:
    async def test_a_non_base64_signature_is_refused(self, redis):
        cert, pem = _cert_and_key()
        args = _signed(pem)
        args["signature"] = "not base64!!"
        with pytest.raises(pop.ProofOfPossessionError, match="base64"):
            await pop.verify_request(cert, **args)

    async def test_a_non_integer_timestamp_is_refused(self, redis):
        cert, pem = _cert_and_key()
        args = _signed(pem)
        args["timestamp"] = "yesterday"
        with pytest.raises(pop.ProofOfPossessionError, match="integer"):
            await pop.verify_request(cert, **args)
