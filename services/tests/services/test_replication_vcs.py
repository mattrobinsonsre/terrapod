"""Replication of VCS connections (#1132).

Two things make this class different from everything replicated before it.

**It holds credentials.** `token` (a GitLab PAT or a GitHub App private key) and
`webhook_secret` are `EncryptedText`, so this is the first class to exercise the
per-node encryption claim for real: the value is read through the ORM already
decrypted, crosses the authenticated peer link as plaintext, and is re-encrypted
under the *receiving* node's own key. Neither node needs the other's key — which
is the whole reason a pair can span two clouds or two KMS tenancies.

**Withholding it fails quietly.** A promotion would look successful and then do
nothing: the poller on the promoted node has no credentials, so every
VCS-connected workspace simply stops seeing pushes and pull requests. Nothing
errors, nothing alerts; work just stops arriving.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.crypto.types import EncryptedText
from terrapod.db.models import VCSConnection
from terrapod.services import replication, replication_registry

VCS = replication_registry.VCS_CONNECTIONS


def _rows_db(rows):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    return db


def _keys_db(keys):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = keys
    db.execute.return_value = result
    return db


def _conn(conn_id="11111111-1111-1111-1111-111111111111", **kw):
    base = {
        "id": conn_id,
        "provider": "github",
        "name": "primary",
        "server_url": "https://api.github.com",
        "token": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----",
        "webhook_secret": "shhh",
        "github_app_id": 42,
        "github_installation_id": 4242,
        "github_account_login": "example-org",
        "github_account_type": "Organization",
        "status": "active",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(kw)
    return VCSConnection(**base)


class TestVCSConnections:
    @pytest.mark.replication_matrix("vcs_connections", "backfill-from-empty")
    async def test_backfill_carries_what_the_poller_needs(self):
        """Adding a second node to a running install has no deltas at all, so
        backfill is how a pair gets its connections in the first place."""
        db = _rows_db([_conn()])

        page = await replication.read_backfill(db, VCS)

        assert page[0]["provider"] == "github"
        assert page[0]["server_url"] == "https://api.github.com"
        assert page[0]["github_installation_id"] == 4242

    @pytest.mark.replication_matrix("vcs_connections", "delta-apply")
    async def test_a_rotated_credential_reaches_the_peer(self):
        """Rotation is the change that matters most here: a follower left on the
        old credential is a follower whose polling breaks at promotion, long
        after anyone remembers rotating anything."""
        db = AsyncMock()
        existing = _conn(token="old-pat")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, VCS, {"id": str(existing.id), "token": "new-pat", "name": "primary"}
        )

        assert existing.token == "new-pat"

    @pytest.mark.replication_matrix("vcs_connections", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _conn()
        db.scalar.return_value = existing
        payload = replication.serialize_row(VCS, existing)

        await replication.apply_upsert(db, VCS, payload)
        await replication.apply_upsert(db, VCS, payload)

        assert existing.name == "primary"
        assert existing.token == payload["token"]
        assert existing.status == "active"

    @pytest.mark.replication_matrix("vcs_connections", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, VCS, str(_conn().id))

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("vcs_connections", "backfill-converges-deletion")
    async def test_a_removed_connection_does_not_survive_a_backfill(self):
        """Deleting a connection is how an operator revokes an integration. If
        backfill did not converge it, the credential would come back to life at
        the failover — which is the same defect #1115 found in API tokens."""
        kept = "11111111-1111-1111-1111-111111111111"
        gone = "22222222-2222-2222-2222-222222222222"
        db = _keys_db([(kept,), (gone,)])

        removed = await replication.reconcile_deletions(db, VCS, {kept})

        assert removed == [gone]


class TestPerNodeEncryption:
    """The claim: decrypt on send, re-encrypt on receive, neither node holding
    the other's key. This is the first replicated class that can test it."""

    @pytest.mark.replication_matrix("vcs_connections", "encrypted-columns")
    def test_the_credential_columns_really_are_encrypted_at_rest(self):
        """If this stopped being true the rest of this class would still pass
        while quietly storing a GitHub App private key in plaintext."""
        from sqlalchemy import inspect as sa_inspect

        encrypted = {
            col.key
            for col in sa_inspect(VCSConnection).column_attrs
            if isinstance(col.expression.type, EncryptedText)
        }

        assert encrypted == {"token", "webhook_secret"}

    def test_serialisation_carries_the_decrypted_value(self):
        """Reading through the ORM is what makes this work — the wire payload
        holds the credential itself, not this node's ciphertext, which the peer
        could not decrypt."""
        conn = _conn(token="a-real-looking-pat")

        payload = replication.serialize_row(VCS, conn)

        assert payload["token"] == "a-real-looking-pat"
        assert payload["webhook_secret"] == "shhh"

    async def test_applying_writes_through_the_encrypted_column(self):
        """The receiving side sets the attribute, so the column type re-encrypts
        under this node's own key. Nothing here knows or needs the peer's."""
        db = AsyncMock()
        existing = _conn(token="theirs")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, VCS, {"id": str(existing.id), "token": "rotated", "webhook_secret": "new"}
        )

        assert existing.token == "rotated"
        assert existing.webhook_secret == "new"

    async def test_a_null_credential_survives_the_round_trip(self):
        """A GitLab connection has no webhook secret and a generic host may have
        no token. `None` must stay `None` rather than becoming the string."""
        conn = _conn(token=None, webhook_secret=None)
        payload = replication.serialize_row(VCS, conn)
        db = AsyncMock()
        target = _conn(token="stale", webhook_secret="stale")
        db.scalar.return_value = target

        await replication.apply_upsert(db, VCS, payload)

        assert target.token is None
        assert target.webhook_secret is None


class TestOpaqueColumnTypes:
    """Regression (#1132). The generic coercion path asked every column for its
    Python type, and SQLAlchemy's base `TypeEngine` *raises* instead of
    returning None while `TypeDecorator` does not forward the call to its impl.
    So the first replicated class holding an `EncryptedText` column took the
    apply path down — with a credential in flight, which is the worst possible
    moment for replication to stop.

    Both halves are fixed and both are asserted: the type now answers, and the
    framework no longer depends on every type being able to.
    """

    def test_encrypted_text_names_its_python_type(self):
        assert EncryptedText().python_type is str

    def test_coercion_tolerates_a_type_that_declines_to_answer(self):
        """A future column type that cannot name itself must degrade to
        'no coercion needed', never to an exception."""
        from sqlalchemy.types import TypeEngine

        assert replication._column_python_type(TypeEngine()) is None

    def test_coercion_still_revives_uuids_and_timestamps(self):
        """The tolerance must not have quietly disabled the coercion the path
        exists to do."""
        import uuid as uuid_mod

        from sqlalchemy import DateTime
        from sqlalchemy.dialects.postgresql import UUID as PGUUID

        assert replication._column_python_type(PGUUID(as_uuid=True)) is uuid_mod.UUID
        assert replication._column_python_type(DateTime(timezone=True)) is datetime


class TestOrdering:
    """Registration order is dependency order, and this class is a dependency of
    several still to come: workspaces, autodiscovery rules and the registry all
    hold a foreign key to it. Inserting any of them before their connection
    exists violates that key, and backfill walks the registry in order."""

    def test_vcs_connections_have_no_dependencies_of_their_own(self):
        from sqlalchemy import inspect as sa_inspect

        table = sa_inspect(VCSConnection).local_table

        assert not list(table.foreign_keys), (
            "vcs_connections gained a foreign key — it must now be registered "
            "after whatever it points at"
        )

    def test_it_is_registered(self):
        assert "vcs_connections" in replication.registered()
