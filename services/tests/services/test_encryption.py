"""Tests for Fernet encryption service (values + state files)."""

import pytest
from cryptography.fernet import Fernet

import terrapod.services.encryption_service as enc


@pytest.fixture(autouse=True)
def setup_encryption():
    """Set up a real Fernet key for encryption tests."""
    key = Fernet.generate_key()
    enc._fernet = Fernet(key)
    enc._initialized = True
    yield
    enc._fernet = None
    enc._initialized = False


@pytest.fixture()
def no_encryption():
    """Disable encryption for tests that need it off."""
    saved = enc._fernet
    enc._fernet = None
    yield
    enc._fernet = saved


# ── Value encryption ────────────────────────────────────────────────────


class TestEncryptDecryptValue:
    def test_roundtrip(self):
        ct = enc.encrypt_value("hello world")
        assert ct != "hello world"
        assert enc.decrypt_value(ct) == "hello world"

    def test_unicode_roundtrip(self):
        text = "Hallo Welt \u00e4\u00f6\u00fc\u00df \U0001f680"
        assert enc.decrypt_value(enc.encrypt_value(text)) == text

    def test_empty_string_roundtrip(self):
        assert enc.decrypt_value(enc.encrypt_value("")) == ""

    def test_wrong_key_raises_value_error(self):
        ct = enc.encrypt_value("secret")
        # Swap to a different key
        enc._fernet = Fernet(Fernet.generate_key())
        with pytest.raises(ValueError, match="key mismatch"):
            enc.decrypt_value(ct)

    def test_corrupt_ciphertext_raises_value_error(self):
        with pytest.raises(ValueError, match="key mismatch"):
            enc.decrypt_value("not-valid-fernet-token")

    def test_encrypt_no_key_raises_runtime_error(self, no_encryption):
        with pytest.raises(RuntimeError, match="not configured"):
            enc.encrypt_value("test")

    def test_decrypt_no_key_raises_runtime_error(self, no_encryption):
        with pytest.raises(RuntimeError, match="not configured"):
            enc.decrypt_value("test")

    def test_different_ciphertexts_each_call(self):
        """Fernet includes a timestamp + random IV, so each encryption differs."""
        a = enc.encrypt_value("same")
        b = enc.encrypt_value("same")
        assert a != b
        assert enc.decrypt_value(a) == enc.decrypt_value(b)


# ── State encryption ───────────────────────────────────────────────────


class TestEncryptDecryptState:
    def test_roundtrip(self):
        plaintext = b'{"version": 4, "serial": 1}'
        encrypted = enc.encrypt_state(plaintext)
        assert encrypted.startswith(enc._STATE_MAGIC)
        assert enc.decrypt_state(encrypted) == plaintext

    def test_magic_prefix_present(self):
        encrypted = enc.encrypt_state(b"state data")
        assert encrypted[:7] == b"TPENC1:"

    def test_legacy_plaintext_passthrough(self):
        """State not starting with magic prefix is returned as-is (legacy)."""
        legacy = b'{"version": 4}'
        assert enc.decrypt_state(legacy) == legacy

    def test_no_key_encrypt_returns_plaintext(self, no_encryption):
        """Without encryption key, encrypt_state is a no-op."""
        data = b"plain state"
        assert enc.encrypt_state(data) == data

    def test_no_key_decrypt_encrypted_state_raises(self, no_encryption):
        """Without key, decrypting encrypted state raises RuntimeError."""
        with pytest.raises(RuntimeError, match="no encryption key"):
            enc.decrypt_state(b"TPENC1:some-encrypted-data")

    def test_corrupt_encrypted_state_raises(self):
        with pytest.raises(ValueError, match="key mismatch"):
            enc.decrypt_state(b"TPENC1:corrupt-data-not-fernet")

    def test_large_state_roundtrip(self):
        large = b"x" * (1024 * 1024)  # 1MB
        assert enc.decrypt_state(enc.encrypt_state(large)) == large

    def test_empty_bytes_roundtrip(self):
        assert enc.decrypt_state(enc.encrypt_state(b"")) == b""


# ── Availability check ─────────────────────────────────────────────────


class TestIsEncryptionAvailable:
    def test_available_with_key(self):
        assert enc.is_encryption_available() is True

    def test_unavailable_without_key(self, no_encryption):
        assert enc.is_encryption_available() is False
