"""Mint the OAuth client a peer node authenticates as (#960, #1161).

The peer link is an OAuth 2.0 `client_credentials` grant: each node registers a
client representing its peer and hands those credentials over. Everything that
consumes the credential shipped — the peer identity class, `/ha/replication/*`,
settings replication, the object-store copier — but **nothing created one**, so
the whole peer link was unconfigurable except by hand-writing a row. This is that
missing step.

Run it on the node that will be **asked**, and give the output to the node that
will be **asking**:

    # On node A, mint the credential node B will use to read from A
    python -m terrapod.cli.peer_client create --client-id peer-b --name "Node B"

    # Then on node B:
    #   ha.peer.url:       https://node-a.example.com
    #   ha.peer.client_id: peer-b
    #   TERRAPOD_HA__PEER__CLIENT_SECRET (from a K8s Secret) = the printed secret

For a pair where either node may be promoted, do it in both directions.

**The secret is printed exactly once and never recoverable** — the same contract
as an API token, and for the same reason: only its SHA-256 is stored. Lost means
rotated, which is what `--rotate` is for. Rotating a peer credential is an
ordinary operational act (a suspected leak, a scheduled rotation), and
hand-editing the table is not an answer to it.

Deliberately a CLI rather than an API endpoint. This is the credential that
bootstraps the trust between two nodes, so it wants to be reachable before any
of the machinery it enables works, and it wants an operator at a terminal reading
a secret off stdout rather than a token in an audit log. An admin endpoint plus
the consumer chain — so a pair can be declared as code — is worth having and is
tracked separately; it is not a substitute for this.
"""

import argparse
import asyncio
import hashlib
import logging
import os
import secrets
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from terrapod.db.models import OAuthClient, now_utc

logger = logging.getLogger("terrapod.peer_client")
logging.basicConfig(level=logging.INFO, format="%(message)s")

#: Long enough that guessing is not a strategy, and URL-safe so it survives every
#: place an operator will paste it (a K8s Secret, a values file, a shell).
_SECRET_BYTES = 32


def _hash(secret: str) -> str:
    """SHA-256, matching what `oauth.py` compares against.

    Not a password hash on purpose: this is a high-entropy machine credential, so
    there is no dictionary to slow down, and the grant is on a request path.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        logger.error("DATABASE_URL is required")
        sys.exit(1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def _run(args: argparse.Namespace) -> int:
    url = _database_url()
    engine = create_async_engine(url, echo=False)

    # Cloud-IAM DB auth (#573), the same way bootstrap and the migrations Job do
    # it — without this the command fails on a password-less IAM-only database.
    from terrapod.db import iam_auth

    iam_auth.register_engine_iam_auth(engine.sync_engine, url)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.command == "list":
            async with session_factory() as session:
                clients = (
                    await session.scalars(select(OAuthClient).order_by(OAuthClient.client_id))
                ).all()
            if not clients:
                logger.info("No peer clients registered.")
                return 0
            logger.info("%-32s %-8s %s", "CLIENT ID", "KIND", "NAME")
            for client in clients:
                logger.info("%-32s %-8s %s", client.client_id, client.kind, client.name)
            return 0

        async with session_factory() as session:
            existing = await session.scalar(
                select(OAuthClient).where(OAuthClient.client_id == args.client_id)
            )

            if existing and not args.rotate:
                # A clear refusal, not a traceback on the unique constraint, and
                # not a silent overwrite — quietly replacing the secret would
                # break the peer that is currently using it.
                logger.error(
                    "A client with id %r already exists. Use --rotate to replace its "
                    "secret (the peer must then be updated with the new one).",
                    args.client_id,
                )
                return 1

            secret = secrets.token_urlsafe(_SECRET_BYTES)

            if existing:
                existing.client_secret_hash = _hash(secret)
                if args.name:
                    existing.name = args.name
                action = "Rotated"
            else:
                session.add(
                    OAuthClient(
                        client_id=args.client_id,
                        client_secret_hash=_hash(secret),
                        name=args.name or args.client_id,
                        kind="peer",
                        created_at=now_utc(),
                    )
                )
                action = "Created"

            await session.commit()

        # Printed after the commit, so a secret is never shown for a client that
        # failed to persist.
        logger.info("")
        logger.info("%s peer client:", action)
        logger.info("  client_id:     %s", args.client_id)
        logger.info("  client_secret: %s", secret)
        logger.info("")
        logger.info("This secret is shown once and is not recoverable. Set it on the PEER")
        logger.info("node as TERRAPOD_HA__PEER__CLIENT_SECRET (via a Kubernetes Secret),")
        logger.info("alongside ha.peer.client_id=%s and ha.peer.url pointing HERE.", args.client_id)
        return 0
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m terrapod.cli.peer_client",
        description="Manage the OAuth clients that peer nodes authenticate as.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Mint a peer client and print its secret once")
    create.add_argument("--client-id", required=True, help="e.g. peer-b")
    create.add_argument("--name", default="", help="Human label, e.g. 'Node B'")
    create.add_argument(
        "--rotate",
        action="store_true",
        help="Replace the secret of an existing client (the peer must be updated too)",
    )

    sub.add_parser("list", help="Show registered peer clients (never their secrets)")
    return parser


async def run(argv: list[str] | None = None) -> int:
    """The command, awaitable.

    Separate from `main` so a test can drive it inside its own event loop —
    `asyncio.run` refuses to nest, and a CLI only reachable through
    `asyncio.run` is a CLI that can only be tested by mocking, which for this
    command would have meant mocking away the one thing that was broken.
    """
    return await _run(_parser().parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
