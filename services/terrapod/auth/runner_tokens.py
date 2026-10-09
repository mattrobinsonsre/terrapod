"""Runner token generation and verification (HMAC-SHA256, stateless).

Short-lived tokens for runner Jobs to authenticate API calls (binary cache,
provider mirror, artifact upload/download). Reuses the signing key derivation
pattern from run_task_service.py.

Two formats, and both are accepted for ever:

    runtok:{run_id}:{phase}:{ttl}:{timestamp}:{hmac_signature}   (current)
    runtok:{run_id}:{ttl}:{timestamp}:{hmac_signature}           (no phase claim)

The phase claim arrived on mainline as the fix for GHSA-xmrf-hxq9-m59m and is
carried here because per-workspace cloud identity (#1901) needs it: the
federation-token mint endpoint takes the phase from the presented token rather
than from a request field, so a plan-phase runner cannot ask for the apply
identity.

**On this release line that mint is the ONLY consumer**, so the claim does not
yet stop a plan-phase token driving any other apply-phase endpoint. Mainline
pairs it with per-endpoint phase enforcement; that half is not here, so the TTL
is still what bounds a leaked token against the artifact, state and cache
routes. Do not read the claim as a general phase boundary on this line.

It is **additive on the wire**: a listener that does not
send a phase when it mints a token gets the older five-field form, which verifies
exactly as before and carries no phase — so a listener image lagging the API
keeps working, and the endpoints that check a phase skip the check rather than
refusing. Absence means "this token makes no claim", never "this token claims the
wrong phase".

The phase travels INSIDE the signed message, so whoever holds the token cannot
edit it — which is the only reason it is worth anything. Field count is what
distinguishes the two forms: a phase is a word and a TTL is digits, but counting
fields does not depend on that staying true.

Run state is NOT checked here, and this module stays pure signature arithmetic
with no I/O. Mainline pairs the verify with a liveness check that refuses a token
for a run that has reached a terminal state; that half is not on this line, so a
token here is good until it expires.
"""

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass

from terrapod.auth.token_signing import get_token_signing_key

#: The phases a Job runs, and so the only values a token may claim. One Job runs
#: one phase (`TP_PHASE`, taken from the listener's own `phase`), which is what
#: makes a per-phase token meaningful.
RUNNER_PHASES: frozenset[str] = frozenset({"plan", "apply"})


@dataclass(frozen=True)
class RunnerTokenClaims:
    """What a verified runner token asserts.

    ``phase`` is None for a token minted without one — see the module docstring.
    Callers MUST read that as "no claim" and fall through, not as a mismatch.
    """

    run_id: str
    phase: str | None


def _get_signing_key() -> bytes:
    """Get the stable HMAC signing key (dedicated secret, or DB-URL fallback)."""
    return get_token_signing_key()


def _sign(message: str) -> str:
    return hmac.new(_get_signing_key(), message.encode(), hashlib.sha256).hexdigest()


def generate_runner_token(
    run_id: str | uuid.UUID,
    ttl: int = 3600,
    *,
    phase: str | None = None,
) -> str:
    """Generate an HMAC-SHA256 runner token.

    Args:
        run_id: The run UUID this token is scoped to.
        ttl: Requested TTL in seconds. Clamped to max_token_ttl_seconds.
        phase: The Job phase the token may act in (``plan`` or ``apply``).
            Omitted — or anything not in ``RUNNER_PHASES`` — mints the older
            form with no phase claim, which every endpoint still accepts.

    Returns:
        ``runtok:{run_id}:{phase}:{ttl}:{ts}:{sig}``, or the five-field form
        without a phase.
    """
    from terrapod.config import load_runner_config

    config = load_runner_config()
    max_ttl = config.max_token_ttl_seconds
    if max_ttl > 0 and ttl > max_ttl:
        ttl = max_ttl

    rid = str(run_id)
    ts = str(int(time.time()))
    if phase in RUNNER_PHASES:
        msg = f"runtok:{rid}:{phase}:{ttl}:{ts}"
        return f"{msg}:{_sign(msg)}"
    msg = f"runtok:{rid}:{ttl}:{ts}"
    return f"{msg}:{_sign(msg)}"


def verify_runner_token_claims(token: str) -> RunnerTokenClaims | None:
    """Verify a runner token and return its claims, or None.

    None for an invalid, expired or tampered token. Both wire forms are accepted;
    the five-field one yields ``phase=None``.
    """
    if not token.startswith("runtok:"):
        return None

    parts = token.split(":")
    phase: str | None
    if len(parts) == 6:
        _, run_id, phase, ttl_str, ts_str, sig = parts
        if phase not in RUNNER_PHASES:
            # Claiming a phase no Job runs. Refuse rather than drop the claim:
            # dropping it would turn an unknown phase into "no claim", and so
            # into a token every phase-checked endpoint accepts.
            return None
        signed = f"runtok:{run_id}:{phase}:{ttl_str}:{ts_str}"
    elif len(parts) == 5:
        _, run_id, ttl_str, ts_str, sig = parts
        phase = None
        signed = f"runtok:{run_id}:{ttl_str}:{ts_str}"
    else:
        return None

    try:
        ts = int(ts_str)
        ttl = int(ttl_str)
    except (ValueError, TypeError):
        return None

    # Check expiry
    if time.time() > ts + ttl:
        return None

    # Verify HMAC
    if not hmac.compare_digest(sig, _sign(signed)):
        return None

    return RunnerTokenClaims(run_id=run_id, phase=phase)


def verify_runner_token(token: str) -> str | None:
    """Verify a runner token and return the run_id if valid.

    Returns None if the token is invalid, expired, or tampered with. Kept beside
    ``verify_runner_token_claims`` for the callers that only need to know whether
    a credential is a runner token at all (rate-limit bucketing).
    """
    claims = verify_runner_token_claims(token)
    return claims.run_id if claims is not None else None
