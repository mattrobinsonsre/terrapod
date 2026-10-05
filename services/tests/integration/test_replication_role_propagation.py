"""A replicated role change reaches the sessions the follower is serving (#1981).

`GHSA-pwrq-j4cv-w7qg` made a role change reach its credentials at every WRITE
path. A follower applying a replicated delta is not a write path, so a demotion
written on one node propagated the row to the other and left the sessions that
node was serving holding the old roles.

These run on a real database and a real Redis because the whole decision is a
before-and-after across TWO tables: `role_name` is part of the primary key, so
one delta is one role while the comparison needs the identity's whole set. A
mocked `db.execute` returning fixed rows cannot tell the before from the after,
which is exactly the shape of test that would pass with the fix removed.
"""

import pytest

from terrapod.auth import sessions
from terrapod.services import replication, replication_registry
from terrapod.services.replication_sync import (
    _note_identity_before_apply,
    _propagate_identity_changes,
)

from .conftest import insert_role

pytestmark = pytest.mark.asyncio

PROVIDER = "local"
EMAIL = "demoted@test.com"

ASSIGNMENTS = replication_registry.ROLE_ASSIGNMENTS
PLATFORM = replication_registry.PLATFORM_ROLE_ASSIGNMENTS


def _event(spec, *, op, provider=PROVIDER, email=EMAIL, role="admin"):
    """The shape `sync_cycle` hands `_note_identity_before_apply`."""
    return {
        "entity-class": spec.name,
        "entity-id": replication.encode_entity_id(spec, [provider, email, role]),
        "op": op,
    }


async def _session_with(roles: list[str]) -> str:
    s = await sessions.create_session(EMAIL, "Demoted", roles, PROVIDER)
    assert await sessions.get_session(s.token) is not None
    return s.token


class TestAReplicatedDemotionRevokesTheFollowersSessions:
    async def test_losing_a_platform_role_ends_the_session(self, app):
        """The reported gap: the row goes, the session must go with it."""
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            db.add(PLATFORM.model(provider_name=PROVIDER, email=EMAIL, role_name="admin"))
            await db.commit()

        token = await _session_with(["admin"])

        async with get_db_session() as db:
            pending: dict[tuple[str, str], set[str]] = {}
            event = _event(PLATFORM, op=replication.DELETE)
            await _note_identity_before_apply(db, event, pending)
            assert pending == {(PROVIDER, EMAIL): {"admin"}}, (
                "the before-set must be read from the database, across both tables"
            )
            await replication.apply_delete(db, PLATFORM, event["entity-id"])
            await db.commit()
            await _propagate_identity_changes(db, pending)

        assert await sessions.get_session(token) is None, (
            "a replicated demotion left the follower's session holding admin"
        )

    async def test_a_custom_role_removal_also_ends_it(self, app):
        # A custom role is FK-constrained to `roles`; a platform role is not,
        # because admin/audit are built in. The row has to exist first.
        await insert_role(None, "dev")

        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            db.add(ASSIGNMENTS.model(provider_name=PROVIDER, email=EMAIL, role_name="dev"))
            await db.commit()

        token = await _session_with(["dev"])

        async with get_db_session() as db:
            pending: dict[tuple[str, str], set[str]] = {}
            event = _event(ASSIGNMENTS, op=replication.DELETE, role="dev")
            await _note_identity_before_apply(db, event, pending)
            await replication.apply_delete(db, ASSIGNMENTS, event["entity-id"])
            await db.commit()
            await _propagate_identity_changes(db, pending)

        assert await sessions.get_session(token) is None


class TestTheSetIsTheUnionOfBothTables:
    async def test_a_pure_addition_in_the_other_table_is_not_read_as_a_loss(self, app):
        """Granting a custom role to someone holding a platform role.

        Nothing is taken away, so this must refresh and not revoke. Read from
        `role_assignments` alone the before-set would be empty and the after-set
        `{dev}` — still a widening, so that mistake hides here. Read from
        `platform_role_assignments` alone the before-set would be `{audit}` and
        the after-set `{audit}`, reporting "unchanged", and the new role would
        never reach the live session. The union is what makes both wrong.
        """
        await insert_role(None, "dev")

        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            db.add(PLATFORM.model(provider_name=PROVIDER, email=EMAIL, role_name="audit"))
            await db.commit()

        token = await _session_with(["audit"])

        # Add a CUSTOM role. Nothing is removed, so the session must survive.
        async with get_db_session() as db:
            pending: dict[tuple[str, str], set[str]] = {}
            event = _event(ASSIGNMENTS, op=replication.UPSERT, role="dev")
            await _note_identity_before_apply(db, event, pending)
            assert pending == {(PROVIDER, EMAIL): {"audit"}}, (
                "the before-set must include the row in the OTHER table"
            )
            await replication.apply_upsert(
                db,
                ASSIGNMENTS,
                {"provider_name": PROVIDER, "email": EMAIL, "role_name": "dev"},
            )
            await db.commit()
            await _propagate_identity_changes(db, pending)

        assert await sessions.get_session(token) is not None, (
            "a pure widening revoked the session — the union read one table only"
        )
        refreshed = await sessions.get_session(token)
        assert "dev" in refreshed.roles, "a widening must reach the live session"


class TestNothingMovedCostsNothing:
    async def test_an_unchanged_identity_keeps_its_session(self, app):
        """A deferred event, or a re-applied one, must not sign anybody out."""
        from terrapod.db.session import get_db_session

        async with get_db_session() as db:
            db.add(PLATFORM.model(provider_name=PROVIDER, email=EMAIL, role_name="admin"))
            await db.commit()

        token = await _session_with(["admin"])

        async with get_db_session() as db:
            pending: dict[tuple[str, str], set[str]] = {}
            await _note_identity_before_apply(db, _event(PLATFORM, op=replication.UPSERT), pending)
            # No apply at all — the event deferred.
            await _propagate_identity_changes(db, pending)

        assert await sessions.get_session(token) is not None
