"""Minting the peer OAuth client (#1161).

An integration test rather than a mocked one, because the bug was that the row was
never written — a test with a mocked session would have passed against the broken
code by construction.

The load-bearing class is the last one: a secret this CLI prints has to be
accepted by the **real `/oauth/token` grant**. Everything else here could pass
while a pair still failed to authenticate, if the hash the CLI writes and the hash
that endpoint compares were not the same function.
"""

import hashlib

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from terrapod.cli import peer_client
from terrapod.db.models import OAuthClient


@pytest.fixture(autouse=True)
def _database_url(monkeypatch):
    """Point the CLI at the same database the app under test uses."""
    from terrapod.config import settings

    monkeypatch.setenv("DATABASE_URL", str(settings.database_url))


async def _run(*argv: str) -> int:
    """Awaited, not `main()` — `asyncio.run` cannot nest inside pytest's loop."""
    return await peer_client.run(list(argv))


def _secret_from(output: str) -> str:
    for line in output.splitlines():
        if "client_secret:" in line:
            return line.split("client_secret:", 1)[1].strip()
    raise AssertionError(f"no client_secret in output:\n{output}")


async def _fetch(client_id: str) -> OAuthClient | None:
    from terrapod.db.session import get_db_session

    async with get_db_session() as session:
        return await session.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id))


class TestCreate:
    async def test_it_writes_a_row_the_grant_can_find(self, app, caplog):
        assert await _run("create", "--client-id", "peer-b", "--name", "Node B") == 0

        row = await _fetch("peer-b")
        assert row is not None, "the bug was that nothing ever created this row"
        assert row.kind == "peer", (
            "the peer identity is its own class — a client landing as any other "
            "kind would not be accepted by the peer gate"
        )
        assert row.name == "Node B"
        assert row.is_active is True

    async def test_the_secret_is_only_hashed_at_rest(self, app, caplog):
        with caplog.at_level("INFO"):
            await _run("create", "--client-id", "peer-b")
        secret = _secret_from(caplog.text)

        row = await _fetch("peer-b")
        assert secret not in row.client_secret_hash
        assert row.client_secret_hash == hashlib.sha256(secret.encode()).hexdigest()

    async def test_the_name_defaults_to_the_client_id(self, app):
        await _run("create", "--client-id", "peer-b")

        assert (await _fetch("peer-b")).name == "peer-b"

    async def test_a_duplicate_is_refused_rather_than_overwritten(self, app):
        """Silently replacing the secret would break the peer currently using it,
        and a traceback on the unique constraint would tell an operator nothing."""
        assert await _run("create", "--client-id", "peer-b") == 0
        before = (await _fetch("peer-b")).client_secret_hash

        assert await _run("create", "--client-id", "peer-b") == 1

        assert (await _fetch("peer-b")).client_secret_hash == before, (
            "the existing secret must survive a refused create"
        )


class TestRotate:
    async def test_rotate_replaces_the_secret_in_place(self, app, caplog):
        await _run("create", "--client-id", "peer-b", "--name", "Node B")
        before = (await _fetch("peer-b")).client_secret_hash

        caplog.clear()
        with caplog.at_level("INFO"):
            assert await _run("create", "--client-id", "peer-b", "--rotate") == 0
        new_secret = _secret_from(caplog.text)

        row = await _fetch("peer-b")
        assert row.client_secret_hash != before
        assert row.client_secret_hash == hashlib.sha256(new_secret.encode()).hexdigest()
        assert row.name == "Node B", "rotating a secret must not clear the label"

    async def test_rotating_a_client_that_does_not_exist_creates_it(self, app):
        """`--rotate` on a fresh id is a create, not an error: an operator who
        cannot remember whether they already minted one should not be punished for
        guessing wrong."""
        assert await _run("create", "--client-id", "peer-new", "--rotate") == 0

        assert await _fetch("peer-new") is not None


class TestList:
    async def test_list_shows_clients_and_never_a_secret(self, app, caplog):
        with caplog.at_level("INFO"):
            await _run("create", "--client-id", "peer-b", "--name", "Node B")
        secret = _secret_from(caplog.text)

        caplog.clear()
        with caplog.at_level("INFO"):
            assert await _run("list") == 0
        out = caplog.text

        assert "peer-b" in out
        assert "Node B" in out
        assert secret not in out, "a listing must never be able to leak a secret"

    async def test_list_on_an_empty_install_says_so(self, app, caplog):
        with caplog.at_level("INFO"):
            assert await _run("list") == 0

        assert "No peer clients" in caplog.text


class TestTheSecretActuallyAuthenticates:
    """Driven through the real `/oauth/token` endpoint.

    This is the assertion the feature lives or dies on: the previous state of the
    world had a working grant and no way to produce a credential it would accept.
    """

    async def test_the_minted_secret_is_accepted_by_the_grant(
        self, app, client: AsyncClient, caplog
    ):
        with caplog.at_level("INFO"):
            await _run("create", "--client-id", "peer-b", "--name", "Node B")
        secret = _secret_from(caplog.text)

        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "peer-b",
                "client_secret": secret,
            },
        )

        assert resp.status_code == 200, (
            f"the CLI minted a credential the grant rejects ({resp.status_code}: "
            f"{resp.text}) — a correctly configured pair would fail to authenticate"
        )
        assert resp.json()["access_token"]

    async def test_a_wrong_secret_is_rejected(self, app, client: AsyncClient):
        await _run("create", "--client-id", "peer-b")

        resp = await client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "peer-b",
                "client_secret": "not-the-secret",
            },
        )

        assert resp.status_code == 401

    async def test_a_rotated_secret_replaces_the_old_one_for_real(
        self, app, client: AsyncClient, caplog
    ):
        """Rotation has to actually invalidate the previous credential, or a
        leaked secret stays usable after the operator believes they revoked it."""
        with caplog.at_level("INFO"):
            await _run("create", "--client-id", "peer-b")
        old = _secret_from(caplog.text)

        caplog.clear()
        with caplog.at_level("INFO"):
            await _run("create", "--client-id", "peer-b", "--rotate")
        new = _secret_from(caplog.text)

        def body(secret: str) -> dict:
            return {
                "grant_type": "client_credentials",
                "client_id": "peer-b",
                "client_secret": secret,
            }

        assert (await client.post("/oauth/token", data=body(new))).status_code == 200
        assert (await client.post("/oauth/token", data=body(old))).status_code == 401
