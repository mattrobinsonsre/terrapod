"""Slack account-linking: signed state + the durable identity binding (#556).

The "connect your Terrapod account" flow:

1. From Slack (`/terrapod link`), Terrapod mints a **signed, single-use,
   short-TTL state token** that encodes the Slack (team, user) — only Terrapod
   (holding the signing key) can produce it, so a user can't forge a link
   binding an arbitrary Slack id to their own account.
2. The user opens the link in their browser and authenticates to Terrapod
   normally (existing session/SSO). The web page then POSTs the state with the
   user's auth; the API verifies + consumes the state and writes the binding to
   the *authenticated* Terrapod identity.

The binding is long-lived **identity**, not entitlement: RBAC is re-checked live
on every Slack-initiated action, so a persistent binding never grants standing
permission.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import SlackIdentityLink

_STATE_TTL_SECONDS = 600  # 10 minutes to complete the link
_NONCE_PREFIX = "tp:slack:linkstate:"


class LinkStateError(Exception):
    """Raised when a link-state token is invalid, expired, or already used."""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload_b64: str) -> str:
    from terrapod.auth.token_signing import get_token_signing_key

    sig = hmac.new(get_token_signing_key(), payload_b64.encode(), hashlib.sha256).digest()
    return _b64u(sig)


async def mint_link_state(team_id: str, user_id: str) -> str:
    """Mint a signed, single-use state token binding this Slack (team, user)."""
    nonce = uuid.uuid4().hex
    payload = {"t": team_id, "u": user_id, "n": nonce, "exp": int(time.time()) + _STATE_TTL_SECONDS}
    payload_b64 = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    token = f"{payload_b64}.{_sign(payload_b64)}"

    # Register the nonce for single-use redemption (TTL mirrors the token expiry).
    from terrapod.redis.client import get_redis_client

    await get_redis_client().set(f"{_NONCE_PREFIX}{nonce}", "1", ex=_STATE_TTL_SECONDS)
    return token


async def verify_and_consume_state(state: str) -> tuple[str, str]:
    """Verify signature + expiry and BURN the nonce (single use). Returns (team, user)."""
    try:
        payload_b64, sig = state.split(".", 1)
    except ValueError as exc:
        raise LinkStateError("malformed link state") from exc

    if not hmac.compare_digest(sig, _sign(payload_b64)):
        raise LinkStateError("bad link-state signature")

    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise LinkStateError("undecodable link state") from exc

    if int(payload.get("exp", 0)) < int(time.time()):
        raise LinkStateError("link state expired")

    nonce = payload.get("n", "")
    from terrapod.redis.client import get_redis_client

    # Atomic single-use: only the first redemption finds the nonce present.
    burned = await get_redis_client().delete(f"{_NONCE_PREFIX}{nonce}")
    if not burned:
        raise LinkStateError("link state already used or expired")

    return str(payload["t"]), str(payload["u"])


async def create_link(
    db: AsyncSession,
    *,
    team_id: str,
    user_id: str,
    email: str,
    via: str = "slash_command",
) -> SlackIdentityLink:
    """Upsert the (team, user) → email binding. Idempotent (re-link updates email)."""
    from terrapod.db.models import now_utc

    existing = (
        await db.execute(
            select(SlackIdentityLink).where(
                SlackIdentityLink.slack_team_id == team_id,
                SlackIdentityLink.slack_user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.terrapod_email = email
        existing.linked_via = via
        existing.linked_at = now_utc()
        link = existing
    else:
        link = SlackIdentityLink(
            slack_team_id=team_id, slack_user_id=user_id, terrapod_email=email, linked_via=via
        )
        db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


async def get_link(db: AsyncSession, team_id: str, user_id: str) -> SlackIdentityLink | None:
    return (
        await db.execute(
            select(SlackIdentityLink).where(
                SlackIdentityLink.slack_team_id == team_id,
                SlackIdentityLink.slack_user_id == user_id,
            )
        )
    ).scalar_one_or_none()


async def unlink(db: AsyncSession, team_id: str, user_id: str) -> int:
    """Remove a binding. Returns the number of rows deleted (0 or 1)."""
    result = await db.execute(
        delete(SlackIdentityLink).where(
            SlackIdentityLink.slack_team_id == team_id,
            SlackIdentityLink.slack_user_id == user_id,
        )
    )
    await db.commit()
    return result.rowcount or 0
