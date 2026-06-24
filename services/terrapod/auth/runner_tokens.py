"""Runner token generation and verification (HMAC-SHA256, stateless).

Short-lived tokens for runner Jobs to authenticate API calls (binary cache,
provider mirror, artifact upload/download). Reuses the signing key derivation
pattern from run_task_service.py.

Format: runtok:{run_id}:{ttl}:{timestamp}:{hmac_signature}
"""

import hashlib
import hmac
import time
import uuid

from terrapod.auth.token_signing import get_token_signing_key


def _get_signing_key() -> bytes:
    """Get the stable HMAC signing key (dedicated secret, or DB-URL fallback)."""
    return get_token_signing_key()


def generate_runner_token(run_id: str | uuid.UUID, ttl: int = 3600) -> str:
    """Generate an HMAC-SHA256 runner token.

    Args:
        run_id: The run UUID this token is scoped to.
        ttl: Requested TTL in seconds. Clamped to max_token_ttl_seconds.

    Returns:
        Token string in format runtok:{run_id}:{ttl}:{ts}:{sig}
    """
    from terrapod.config import load_runner_config

    config = load_runner_config()
    max_ttl = config.max_token_ttl_seconds
    if max_ttl > 0 and ttl > max_ttl:
        ttl = max_ttl

    rid = str(run_id)
    ts = str(int(time.time()))
    msg = f"runtok:{rid}:{ttl}:{ts}".encode()
    sig = hmac.new(_get_signing_key(), msg, hashlib.sha256).hexdigest()
    return f"runtok:{rid}:{ttl}:{ts}:{sig}"


def verify_runner_token(token: str) -> str | None:
    """Verify a runner token and return the run_id if valid.

    Returns None if the token is invalid, expired, or tampered with.
    """
    if not token.startswith("runtok:"):
        return None

    parts = token.split(":")
    if len(parts) != 5:
        return None

    _, run_id, ttl_str, ts_str, sig = parts

    try:
        ttl = int(ttl_str)
        ts = int(ts_str)
    except (ValueError, TypeError):
        return None

    # Check expiry
    if time.time() > ts + ttl:
        return None

    # Verify HMAC
    msg = f"runtok:{run_id}:{ttl}:{ts_str}".encode()
    expected = hmac.new(_get_signing_key(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    return run_id
