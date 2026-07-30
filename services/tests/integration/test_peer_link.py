"""Reconciling the configured peer credential into a persisted row (#1169).

Integration rather than mocked, for the same reason the CA's tests are: the two
things that can go wrong here are a Postgres unique-constraint race between
replicas and a stored hash that silently fails to move. A mocked session proves
neither — it would pass against code that does not write at all.

The load-bearing cases are the last two classes: a config-supplied secret that
CHANGES must reach the database (or rotating by editing the chart silently does
nothing while the operator believes they have rotated), and a client id named
WITHOUT a secret must not mint (a secret generated at startup is hashed
immediately and could never be read, leaving a credential nobody can hand to
their peer).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from terrapod.db.models import OAuthClient
from terrapod.db.session import get_db_session
from terrapod.services import peer_link


def _cfg(client_id: str = "", client_secret: str = "", name: str = ""):
    """Just the `ha.peer.inbound` shape the reconcile reads."""
    return SimpleNamespace(
        ha=SimpleNamespace(
            peer=SimpleNamespace(
                inbound=SimpleNamespace(client_id=client_id, client_secret=client_secret, name=name)
            )
        )
    )


async def _reconcile(**kwargs) -> str | None:
    with patch("terrapod.services.peer_link.settings", _cfg(**kwargs)):
        async with get_db_session() as db:
            return await peer_link.reconcile_inbound_client(db)


async def _row(client_id: str) -> OAuthClient | None:
    async with get_db_session() as db:
        return await db.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id))


class TestNothingConfigured:
    async def test_a_single_node_install_is_left_entirely_alone(self, app):
        # The overwhelming majority of installs. It must not create rows, and it
        # must not take the advisory lock on every startup for nothing.
        assert await _reconcile() is None

        async with get_db_session() as db:
            assert (await db.scalars(select(OAuthClient))).all() == []


class TestDeclarativeSetup:
    async def test_a_configured_secret_materialises_the_row(self, app):
        outcome = await _reconcile(client_id="peer-b", client_secret="s3cret", name="Node B")

        assert outcome == "created"
        row = await _row("peer-b")
        assert row is not None
        assert row.kind == "peer", "must land as a peer, not a general machine client"
        assert row.name == "Node B"
        assert row.created_by == "config", "so an audit can tell config from a human"
        assert row.client_secret_hash == peer_link.hash_secret("s3cret")

    async def test_the_secret_is_hashed_never_stored_raw(self, app):
        await _reconcile(client_id="peer-b", client_secret="s3cret")

        assert "s3cret" not in (await _row("peer-b")).client_secret_hash

    async def test_reconciling_again_with_the_same_config_changes_nothing(self, app):
        await _reconcile(client_id="peer-b", client_secret="s3cret")

        assert await _reconcile(client_id="peer-b", client_secret="s3cret") == "unchanged"

    async def test_the_grant_accepts_the_declared_secret(self, app, client):
        """The assertion the feature lives or dies on: a link set up purely from
        config must authenticate, with no CLI step anywhere."""
        await _reconcile(client_id="peer-b", client_secret="s3cret")

        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "peer-b",
                "client_secret": "s3cret",
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["access_token"]


class TestRotationByChart:
    """If the reconcile only created-when-absent, editing the chart to rotate
    would silently do nothing — worse than not offering it."""

    async def test_a_changed_secret_reaches_the_database(self, app):
        await _reconcile(client_id="peer-b", client_secret="old")

        assert await _reconcile(client_id="peer-b", client_secret="new") == "rotated"

        assert (await _row("peer-b")).client_secret_hash == peer_link.hash_secret("new")

    async def test_the_previous_secret_stops_working_immediately(self, app, client):
        await _reconcile(client_id="peer-b", client_secret="old")
        await _reconcile(client_id="peer-b", client_secret="new")

        def body(secret: str) -> dict:
            return {
                "grant_type": "client_credentials",
                "client_id": "peer-b",
                "client_secret": secret,
            }

        assert (await client.post("/oauth/token", data=body("new"))).status_code == 200
        assert (await client.post("/oauth/token", data=body("old"))).status_code == 401

    async def test_re_declaring_a_revoked_credential_reactivates_it(self, app):
        # An operator who has put the credential back in config intends it to
        # work; leaving it revoked would be a confusing way to fail.
        await _reconcile(client_id="peer-b", client_secret="s3cret")
        async with get_db_session() as db:
            row = await db.scalar(select(OAuthClient).where(OAuthClient.client_id == "peer-b"))
            row.is_active = False
            await db.commit()

        await _reconcile(client_id="peer-b", client_secret="s3cret")

        assert (await _row("peer-b")).is_active is True


class TestNamedWithoutASecret:
    """Minting here would produce a credential nobody can read."""

    async def test_it_does_not_invent_a_secret(self, app):
        outcome = await _reconcile(client_id="peer-b")

        assert outcome == "awaiting-secret"
        assert await _row("peer-b") is None, (
            "a row minted at startup is hashed immediately, so the operator "
            "would hold a credential they cannot give their peer"
        )

    async def test_it_leaves_a_cli_minted_credential_untouched(self, app):
        # The fallback path: `peer_client` minted it and showed it once. A
        # subsequent startup must not clobber what the operator is using.
        async with get_db_session() as db:
            db.add(
                OAuthClient(
                    client_id="peer-b",
                    client_secret_hash=peer_link.hash_secret("minted-by-cli"),
                    name="Node B",
                    kind="peer",
                    created_by="admin",
                )
            )
            await db.commit()

        assert await _reconcile(client_id="peer-b") == "unchanged"

        assert (await _row("peer-b")).client_secret_hash == peer_link.hash_secret("minted-by-cli")


class TestConcurrentReplicas:
    async def test_replicas_starting_together_do_not_race(self, app):
        """The CA hit exactly this and it was a live bug (#1060): several
        replicas each see "no row", each insert, and whichever loses the unique
        constraint crash-loops. The advisory lock makes them queue."""
        outcomes = await asyncio.gather(
            *(_reconcile(client_id="peer-b", client_secret="s3cret") for _ in range(5))
        )

        assert outcomes.count("created") == 1, outcomes
        assert all(o in ("created", "unchanged") for o in outcomes), outcomes

        async with get_db_session() as db:
            rows = (
                await db.scalars(select(OAuthClient).where(OAuthClient.client_id == "peer-b"))
            ).all()
        assert len(rows) == 1


class TestStatus:
    async def test_an_unconfigured_node_reports_not_configured(self, app):
        with patch("terrapod.services.peer_link.settings", _cfg()):
            async with get_db_session() as db:
                status = await peer_link.inbound_status(db)

        assert status["configured"] is False
        assert status["client-id"] is None

    async def test_a_named_but_unmaterialised_credential_is_not_configured(self, app):
        # Writing a name in a values file is not the same as having a working
        # credential, and the status must not imply that it is.
        with patch("terrapod.services.peer_link.settings", _cfg(client_id="peer-b")):
            async with get_db_session() as db:
                status = await peer_link.inbound_status(db)

        assert status["configured"] is False
        assert status["client-id"] == "peer-b"

    async def test_a_materialised_credential_reports_configured(self, app):
        await _reconcile(client_id="peer-b", client_secret="s3cret")

        with patch(
            "terrapod.services.peer_link.settings", _cfg(client_id="peer-b", client_secret="s")
        ):
            async with get_db_session() as db:
                status = await peer_link.inbound_status(db)

        assert status["configured"] is True
        assert status["active"] is True

    async def test_the_status_never_carries_the_secret_or_its_hash(self, app):
        await _reconcile(client_id="peer-b", client_secret="s3cret")

        with patch(
            "terrapod.services.peer_link.settings", _cfg(client_id="peer-b", client_secret="s")
        ):
            async with get_db_session() as db:
                status = await peer_link.inbound_status(db)

        blob = repr(status)
        assert "s3cret" not in blob
        assert peer_link.hash_secret("s3cret") not in blob
