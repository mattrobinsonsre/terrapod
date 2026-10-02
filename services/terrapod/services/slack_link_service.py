"""Slack account-linking: signed state + the durable identity binding (#556).

The "connect your Terrapod account" flow:

1. From Slack (`/terrapod link`), Terrapod mints a **signed, single-use,
   short-TTL state token** that encodes the Slack (team, user). Only Terrapod can
   produce it, so nobody forges a state binding an arbitrary Slack id.

   That signature is **not** what protects the account being bound, and reading it
   as though it were is how `GHSA-5899-fm2p-88x3` happened. The state is minted for
   whoever ran `/terrapod link` and the binding goes to whoever is *authenticated*
   when the confirm is POSTed — so the dangerous direction is the reverse of the
   obvious one: an attacker mints a perfectly valid state for their own Slack
   identity and sends the URL to a victim, whose session completes the bind. What
   defends against that is the confirm screen **naming a recognisable human**
   (`describe_slack_identity` below), not the signature.
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

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import SlackIdentityLink

logger = structlog.get_logger(__name__)

_STATE_TTL_SECONDS = 600  # 10 minutes to complete the link
_NONCE_PREFIX = "tp:slack:linkstate:"


class LinkStateError(Exception):
    """Raised when a link-state token is invalid, expired, or already used."""


def _bot_client():
    """A Slack web client, built per call like `slack_notify_service._bot_client`.

    Matching that pattern rather than reaching into `slack_service._socket_client`
    keeps the lookup working when socket mode is not connected — a link confirm can
    arrive while the socket is reconnecting, and failing it then would be a worse
    outcome than one extra HTTP session.
    """
    from slack_sdk.web.async_client import AsyncWebClient

    from terrapod.config import settings

    return AsyncWebClient(token=settings.slack.bot_token)


async def describe_slack_identity(team_id: str, user_id: str) -> dict[str, str]:
    """Human-readable names for a Slack (team, user), for the confirm screen.

    This is the fix for `GHSA-5899-fm2p-88x3`, so it is worth being explicit about
    why a name rather than an id. The screen used to say "link `U04F2AB3C` in
    `T01XYZ` to you@example.com". Nobody can tell whether `U04F2AB3C` is their own
    Slack account, so a victim sent that URL by an attacker had nothing to go on and
    confirmed a binding of the attacker's identity to their account. "Link **Dave
    Smith** (@dave) in **Acme Corp**" is wrong at a glance to anyone who is not Dave.

    **Best-effort by design.** `users.info` needs the `users:read` scope and
    `team.info` needs `team:read`; an operator who has not granted them, or a Slack
    API blip, must not make linking impossible — so a failure returns the ids and
    says the names are unavailable, and the confirm page is responsible for telling
    the user it could not name them. Returning a plausible-looking blank would be
    worse than returning nothing: it would read as a name the victim does not
    recognise either way.

    Keys: `user-name` (handle), `user-real-name`, `team-name`, and `resolved`
    ("true"/"false") so a caller never has to guess whether a blank is an absent
    display name or a failed lookup.
    """
    out = {"user-name": "", "user-real-name": "", "team-name": "", "resolved": "false"}
    from terrapod.config import settings

    if not settings.slack.enabled or not settings.slack.bot_token:
        return out
    try:
        client = _bot_client()
        info = await client.users_info(user=user_id)
        profile = (info.get("user") or {}) if info else {}
        out["user-name"] = profile.get("name") or ""
        out["user-real-name"] = (
            (profile.get("profile") or {}).get("real_name") or profile.get("real_name") or ""
        )
        try:
            team = await client.team_info(team=team_id)
            out["team-name"] = ((team.get("team") or {}) if team else {}).get("name") or ""
        except Exception as exc:  # noqa: BLE001 — the user name is the load-bearing half
            logger.info("slack.team_info_failed", team=team_id, err=str(exc))
        out["resolved"] = "true" if (out["user-name"] or out["user-real-name"]) else "false"
    except Exception as exc:  # noqa: BLE001 — never block a link on a lookup
        logger.warning(
            "slack.users_info_failed",
            user=user_id,
            err=str(exc),
            detail="the confirm screen will show opaque ids; grant users:read",
        )
    return out


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload_b64: str) -> str:
    from terrapod.auth.token_signing import get_token_signing_key

    sig = hmac.new(get_token_signing_key(), payload_b64.encode(), hashlib.sha256).digest()
    return _b64u(sig)


async def mint_link_state(team_id: str, user_id: str, response_url: str = "") -> str:
    """Mint a signed, single-use state token binding this Slack (team, user).

    The originating slash command's ``response_url`` is stashed under the nonce so
    the link-completion handler can post a confirmation back to the same Slack
    conversation — no extra Slack scope needed.
    """
    nonce = uuid.uuid4().hex
    payload = {"t": team_id, "u": user_id, "n": nonce, "exp": int(time.time()) + _STATE_TTL_SECONDS}
    payload_b64 = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    token = f"{payload_b64}.{_sign(payload_b64)}"

    # Register the nonce for single-use redemption (TTL mirrors the token expiry).
    # Value is the response_url ("-" sentinel when absent).
    from terrapod.redis.client import get_redis_client

    await get_redis_client().set(
        f"{_NONCE_PREFIX}{nonce}", response_url or "-", ex=_STATE_TTL_SECONDS
    )
    return token


def _decode_verified_payload(state: str) -> dict:
    """Verify the signature + expiry of a link state and return its payload.
    Does NOT touch the single-use nonce — callers decide whether to peek or burn."""
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
    return payload


async def peek_link_state(state: str) -> tuple[str, str]:
    """Verify the state and confirm its nonce is still live WITHOUT consuming it —
    so the browser confirm screen can show *which* Slack identity a state would
    bind before the user commits (confused-deputy defence). Returns
    ``(team_id, user_id)``. Consumption still happens in
    ``verify_and_consume_state`` when the user confirms."""
    payload = _decode_verified_payload(state)
    nonce = payload.get("n", "")
    from terrapod.redis.client import get_redis_client

    if not await get_redis_client().exists(f"{_NONCE_PREFIX}{nonce}"):
        raise LinkStateError("link state already used or expired")
    return str(payload["t"]), str(payload["u"])


async def verify_and_consume_state(state: str) -> tuple[str, str, str]:
    """Verify signature + expiry and BURN the nonce (single use).

    Returns ``(team_id, user_id, response_url)`` — response_url is "" when none was
    captured at mint time.
    """
    payload = _decode_verified_payload(state)
    nonce = payload.get("n", "")
    from terrapod.redis.client import get_redis_client

    # Atomic single-use: GETDEL returns the stored value (response_url) and removes
    # the nonce in one op; a second redemption finds nothing.
    stored = await get_redis_client().getdel(f"{_NONCE_PREFIX}{nonce}")
    if stored is None:
        raise LinkStateError("link state already used or expired")
    if isinstance(stored, bytes):
        stored = stored.decode()
    response_url = "" if stored == "-" else stored

    return str(payload["t"]), str(payload["u"]), response_url


async def post_response_url(response_url: str, text: str, *, replace_original: bool = True) -> None:
    """Best-effort follow-up to a Slack response_url (no scope needed).

    ``replace_original=True`` overwrites the message the response_url belongs to
    (right for a slash command's own ephemeral reply). For a button click on a
    *posted channel message*, pass ``replace_original=False`` — otherwise the
    ephemeral nudge would clobber the shared approval message for everyone.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                response_url,
                json={
                    "response_type": "ephemeral",
                    "replace_original": replace_original,
                    "text": text,
                },
            )
    except Exception:  # noqa: BLE001
        pass


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
