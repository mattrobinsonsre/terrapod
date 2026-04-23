"""Tests for password hashing and strength validation."""

import pytest

from terrapod.auth.passwords import hash_password, validate_password_strength, verify_password


class TestPasswordHashing:
    async def test_hash_and_verify_roundtrip(self):
        password = "correct-horse-battery-staple-42!"
        hashed = await hash_password(password)

        assert hashed != password
        assert hashed.startswith("pbkdf2:sha256:100000$")
        assert await verify_password(password, hashed)

    async def test_wrong_password_fails(self):
        hashed = await hash_password("correct-password-here-99!")
        assert not await verify_password("wrong-password-here-99!", hashed)

    async def test_different_hashes_for_same_password(self):
        """Each hash should use a different random salt."""
        h1 = await hash_password("same-password-twice-42!")
        h2 = await hash_password("same-password-twice-42!")
        assert h1 != h2

    async def test_verify_invalid_hash_format(self):
        assert not await verify_password("password", "not-a-valid-hash")
        assert not await verify_password("password", "a$b")
        assert not await verify_password("password", "wrong:method:0$salt$hash")

    async def test_verify_empty_hash(self):
        assert not await verify_password("password", "")


class TestPasswordStrength:
    def test_strong_password_passes(self):
        result = validate_password_strength("correct-horse-battery-staple-42!")
        assert result == "correct-horse-battery-staple-42!"

    def test_weak_password_raises(self):
        with pytest.raises(ValueError):
            validate_password_strength("password")

    def test_common_password_raises(self):
        with pytest.raises(ValueError):
            validate_password_strength("123456")

    def test_too_long_password_raises(self):
        with pytest.raises(ValueError, match="72 characters"):
            validate_password_strength("a" * 73)

    def test_user_inputs_penalize_score(self):
        # A password containing the user's email should be penalized
        with pytest.raises(ValueError):
            validate_password_strength("admin@example.com", user_inputs=["admin@example.com"])
