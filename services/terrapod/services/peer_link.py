"""Reconcile the configured peer credential into a persisted row (#1169).

Peering used to be split across two systems for no designed reason: the peer URL
was config because it was needed at startup, and the credential was a database
row because it needed hashing and rotation. The boundary fell between two halves
of one thing — a URL without its credential is inert, a credential without its
URL unreachable — so an operator set up *one* link across two lifecycles.

This applies the pattern Terrapod already uses for the CA: **config states the
intent, the database holds the materialised state, startup reconciles one into
the other.** Config stays the input rather than becoming a second writer, so the
two cannot disagree about what is configured.

There are two credentials per node and only one of them belongs here:

- **outbound** (``ha.peer.client_id`` / ``client_secret``) — what this node
  presents when it pulls. A credential it holds but does not own, so there is
  nothing to persist; it is treated exactly like an SSO client secret.
- **inbound** (``ha.peer.inbound.*``) — what this node accepts when its peer
  pulls from it. It owns this one: hashed at rest, rotatable. That is what this
  module materialises.

Supply the inbound secret and setup is fully declarative — the same value as the
peer's outbound secret, in a Kubernetes Secret on each node, and the link needs
no CLI and no UI.
"""

import hashlib
import hmac

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import OAuthClient, now_utc

logger = structlog.get_logger(__name__)

#: Its own lock key, distinct from the CA's. Two singletons initialised in the
#: same startup must not serialise against each other.
_PEER_LINK_ADVISORY_LOCK = 0x7E44_0002


def hash_secret(secret: str) -> str:
    """SHA-256, matching what the `client_credentials` grant compares against.

    Not a password hash on purpose: this is a high-entropy machine credential,
    so there is no dictionary to slow down, and the grant is on a request path.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


async def reconcile_inbound_client(db: AsyncSession) -> str | None:
    """Make the database match the configured inbound credential.

    Returns a short outcome for logging/tests: ``created``, ``rotated``,
    ``unchanged``, ``awaiting-secret``, or ``None`` when nothing is configured.

    Four cases, and the interesting ones are the last two:

    - **Nothing configured** — the ordinary single-node install. Do nothing.
    - **Client id and secret configured** — ensure a row exists with that hash.
    - **Client id configured, secret CHANGED** — update the stored hash. If this
      only created-when-absent, rotating by editing the chart would silently do
      nothing while the operator believed they had rotated, which is worse than
      not offering it at all.
    - **Client id configured, no secret** — deliberately do nothing. A secret
      minted here is hashed immediately and nobody can ever read it, so the
      operator would hold a credential they cannot give their peer. `peer_client`
      mints it instead, and shows it exactly once, to a human.
    """
    cfg = settings.ha.peer.inbound
    if not cfg.client_id:
        return None

    # Serialise check-then-write across replicas, exactly as `init_ca` does.
    # Without it several replicas starting together each see "no row" and each
    # insert one, and the unique constraint turns a startup into a crash loop
    # for whichever lost. The lock releases when this transaction commits.
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _PEER_LINK_ADVISORY_LOCK})

    existing = await db.scalar(select(OAuthClient).where(OAuthClient.client_id == cfg.client_id))

    if not cfg.client_secret:
        # Named but not supplied. Say so once at startup rather than leaving an
        # operator to discover the link is dead when they try to fail over.
        if existing is None:
            logger.warning(
                "Inbound peer credential is configured by id but has no secret; "
                "mint it with `python -m terrapod.cli.peer_client create` or set "
                "ha.peer.inbound.client_secret",
                client_id=cfg.client_id,
            )
            await db.commit()
            return "awaiting-secret"
        await db.commit()
        return "unchanged"

    expected = hash_secret(cfg.client_secret)

    if existing is None:
        db.add(
            OAuthClient(
                client_id=cfg.client_id,
                client_secret_hash=expected,
                name=cfg.name or cfg.client_id,
                kind="peer",
                created_by="config",
                created_at=now_utc(),
            )
        )
        await db.commit()
        logger.info("Created inbound peer credential from config", client_id=cfg.client_id)
        return "created"

    outcome = "unchanged"
    # compare_digest rather than `!=`: both sides are hex digests of a secret,
    # and there is no reason to leak timing here even on a startup path.
    if not hmac.compare_digest(existing.client_secret_hash, expected):
        existing.client_secret_hash = expected
        outcome = "rotated"
    if cfg.name and existing.name != cfg.name:
        existing.name = cfg.name
    # A credential the operator has re-declared in config is one they intend to
    # work; silently leaving it revoked would be a confusing way to fail.
    if not existing.is_active:
        existing.is_active = True
        outcome = "rotated" if outcome == "rotated" else "created"

    await db.commit()
    if outcome != "unchanged":
        logger.info("Updated inbound peer credential from config", client_id=cfg.client_id)
    return outcome


async def inbound_status(db: AsyncSession) -> dict:
    """What `/ha` reports about the inbound credential.

    Never the secret, and never its hash — only whether the link is set up and
    whether it has been used, which is how an operator tells a live credential
    from a forgotten one.
    """
    cfg = settings.ha.peer.inbound
    if not cfg.client_id:
        return {"configured": False, "client-id": None, "active": None, "last-used-at": None}

    row = await db.scalar(select(OAuthClient).where(OAuthClient.client_id == cfg.client_id))
    return {
        # Configured means the credential exists and can actually be presented —
        # not merely that a name was written in a values file.
        "configured": row is not None,
        "client-id": cfg.client_id,
        "active": row.is_active if row else None,
        "last-used-at": row.last_used_at if row else None,
    }
