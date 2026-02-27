"""Password hashing and strength validation utilities."""

import hashlib
import secrets

from zxcvbn import zxcvbn

# Minimum zxcvbn score (0-4). Score 3 = "safely unguessable: moderate protection
# from offline slow-hash scenario". Score 4 = "very unguessable".
MIN_ZXCVBN_SCORE = 3


def validate_password_strength(password: str, user_inputs: list[str] | None = None) -> str:
    """Validate password strength using zxcvbn.

    Uses Dropbox's zxcvbn library which estimates actual crack time by detecting
    dictionary words, l33t substitutions, keyboard patterns, repeated characters,
    sequences, dates, and other common patterns.

    Requires a zxcvbn score of at least 3 (out of 4).

    Args:
        password: The password to validate.
        user_inputs: Optional list of user-specific strings (email, name) that
            should penalize the score if found in the password.

    Returns the password if valid, raises ValueError with feedback otherwise.
    """
    if len(password) > 72:
        raise ValueError("Password must be 72 characters or fewer")

    result = zxcvbn(password, user_inputs=user_inputs or [])

    if result["score"] < MIN_ZXCVBN_SCORE:
        feedback = result.get("feedback", {})
        warning = feedback.get("warning", "")
        suggestions = feedback.get("suggestions", [])

        parts: list[str] = []
        if warning:
            parts.append(warning)
        if suggestions:
            parts.extend(suggestions)

        message = ". ".join(parts) if parts else "Password is too weak"
        raise ValueError(message)

    return password


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt-style salted hashing.

    Uses PBKDF2-SHA256 with a random salt.
    """
    salt = secrets.token_hex(16)
    hash_bytes = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt.encode(),
        iterations=100000,
    )
    hash_hex = hash_bytes.hex()
    return f"pbkdf2:sha256:100000${salt}${hash_hex}"


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a password against its hash.

    Returns True if the password matches, False otherwise.
    """
    try:
        parts = password_hash.split("$")
        if len(parts) != 3:
            return False

        method_info = parts[0]
        salt = parts[1]
        stored_hash = parts[2]

        if not method_info.startswith("pbkdf2:sha256:"):
            return False

        iterations = int(method_info.split(":")[-1])

        hash_bytes = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            salt.encode(),
            iterations=iterations,
        )
        computed_hash = hash_bytes.hex()

        return secrets.compare_digest(computed_hash, stored_hash)
    except (ValueError, IndexError):
        return False
