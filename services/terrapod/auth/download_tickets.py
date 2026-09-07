"""Short-lived download tickets — stateless HMAC cap-tokens.

Why these exist
---------------
The default API contract is that download endpoints (e.g.
`GET /configuration-versions/{cv_id}/download`) take a `Bearer` token
in the `Authorization` header and return blob bytes. That works for
machine clients and for in-app `apiFetch` calls, but it doesn't let
the *browser* stream a download natively to disk via its save dialog
— browsers can't inject Authorization headers into plain navigation,
so the only auth they carry automatically is cookies (which we don't
do) or tokens embedded in the URL itself.

This module generates the latter: short-lived, stateless cap tokens
that callers exchange (server-side, with their normal Bearer token)
for a public URL of the form
`/api/terrapod/v1/.../download-by-ticket/{ticket}`. Browser navigates plainly,
the ticket *is* the auth, the response streams to disk via
`Content-Disposition: attachment` like any download has done forever.

Stateless: no Redis, no DB, no expiry sweep. The HMAC plus the
embedded expiry timestamp covers it. Same key derivation as
`runner_tokens.py` — reuse instead of inventing a parallel signing
key.

Format
------
``dlticket:{resource_kind}:{resource_id}:{user_email_b64}:{ttl}:{ts}:{sig}``

* `resource_kind` — string discriminator the verifier uses to dispatch
  ("cv" today; pluggable for state versions etc. later).
* `resource_id` — the resource's UUID (without prefix).
* `user_email_b64` — the email of the user who minted the ticket.
  We don't *require* it for verification, but the download endpoint
  logs it for audit ("which user's session minted this download?").
  base64url-encoded so the email's `@` doesn't trip the colon-split.
* `ttl` — requested TTL in seconds. Clamped server-side at mint time.
* `ts` — unix timestamp at mint.
* `sig` — HMAC-SHA256 over `dlticket:{kind}:{id}:{email_b64}:{ttl}:{ts}`.

The whole token is bound to one resource. A ticket minted for CV X
cannot be used to download CV Y. Single-use isn't enforced (would
need state) — the safety story is the short TTL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

# Default TTL — long enough for a user to navigate, short enough that
# a leaked ticket has near-zero blast radius. 5 min covers slow
# clicks; tighter than runner tokens because there's no retry loop.
DEFAULT_TTL_SECONDS = 300

# Hard cap. A caller asking for more is silently clamped.
MAX_TTL_SECONDS = 1800


def _get_signing_key() -> bytes:
    """Get the stable HMAC signing key (shared with runner + run-task tokens).

    Uses the dedicated `token_signing_key` secret when configured, else falls
    back to `sha256(database_url)` (see auth.token_signing) — so a configured
    secret decouples download-ticket forgery from the database credentials too.
    """
    from terrapod.auth.token_signing import get_token_signing_key

    return get_token_signing_key()


def _b64encode(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64decode(s: str) -> str:
    # Re-pad — urlsafe_b64decode demands the trailing `=`s back.
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad)).decode("utf-8")


def mint_ticket(
    resource_kind: str,
    resource_id: str,
    user_email: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Generate a short-lived download ticket for the given resource."""
    if ttl_seconds <= 0:
        ttl_seconds = DEFAULT_TTL_SECONDS
    if ttl_seconds > MAX_TTL_SECONDS:
        ttl_seconds = MAX_TTL_SECONDS

    email_b64 = _b64encode(user_email or "")
    ts = str(int(time.time()))
    msg = f"dlticket:{resource_kind}:{resource_id}:{email_b64}:{ttl_seconds}:{ts}".encode()
    sig = hmac.new(_get_signing_key(), msg, hashlib.sha256).hexdigest()
    return f"dlticket:{resource_kind}:{resource_id}:{email_b64}:{ttl_seconds}:{ts}:{sig}"


class TicketPayload:
    """Verified payload extracted from a ticket."""

    __slots__ = ("resource_kind", "resource_id", "user_email", "expires_at")

    def __init__(self, resource_kind: str, resource_id: str, user_email: str, expires_at: int):
        self.resource_kind = resource_kind
        self.resource_id = resource_id
        self.user_email = user_email
        self.expires_at = expires_at


def verify_ticket(ticket: str) -> TicketPayload | None:
    """Verify a download ticket and return the payload if valid.

    Returns None if the ticket is malformed, expired, or has a bad
    signature. Constant-time comparison on the signature.
    """
    if not ticket.startswith("dlticket:"):
        return None

    parts = ticket.split(":")
    if len(parts) != 7:
        return None

    _, kind, rid, email_b64, ttl_str, ts_str, sig = parts

    try:
        ttl = int(ttl_str)
        ts = int(ts_str)
    except (ValueError, TypeError):
        return None

    expires_at = ts + ttl
    if time.time() > expires_at:
        return None

    msg = f"dlticket:{kind}:{rid}:{email_b64}:{ttl}:{ts_str}".encode()
    expected = hmac.new(_get_signing_key(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        email = _b64decode(email_b64)
    except (ValueError, UnicodeDecodeError):
        # Bogus email blob — the HMAC said it was authentic, but the
        # encoding is junk. Treat as a malformed ticket.
        return None

    return TicketPayload(
        resource_kind=kind,
        resource_id=rid,
        user_email=email,
        expires_at=expires_at,
    )
