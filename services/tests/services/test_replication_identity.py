"""Replication of identity and access (#1119).

These are the classes that decide whether a promoted node is usable by anyone.
Withholding them leaves it holding the whole estate with nobody able to touch
it — so they come before everything that references them, and each carries the
full matrix.

Two of them exist to exercise properties the earlier classes could not:

- **`role_assignments` and `platform_role_assignments` have composite keys.** A
  node with users and roles but not the mapping between them has nobody with
  any permissions.
- **`api_tokens` is what #1115 was really about.** Revocation is a hard DELETE,
  so it converges only because the delta path records deletes *and* backfill
  reconciles. Without both, an offboarded person's token comes back to life at
  a failover, weeks after the revocation was performed and confirmed.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import APIToken, PlatformRoleAssignment, Role, RoleAssignment, User
from terrapod.services import replication, replication_registry

USERS = replication_registry.USERS
ROLES = replication_registry.ROLES
ASSIGNMENTS = replication_registry.ROLE_ASSIGNMENTS
PLATFORM = replication_registry.PLATFORM_ROLE_ASSIGNMENTS
TOKENS = replication_registry.API_TOKENS


def _rows_db(rows):
    """A db whose `execute` yields these ORM rows."""
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute.return_value = result
    return db


def _keys_db(keys):
    """A db whose `execute` yields these key tuples (the reconcile path)."""
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = keys
    db.execute.return_value = result
    return db


def _token(token_id="at-1", **kw):
    base = {
        "id": token_id,
        "token_hash": "h" * 64,
        "description": "",
        "kind": "interactive",
        "bound_to": "a@example.com",
        "created_by": "admin",
        "token_type": "user",
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return APIToken(**base)


class TestUsers:
    """Keyed by email rather than a UUID."""

    @pytest.mark.replication_matrix("users", "backfill-from-empty")
    async def test_backfill_serializes_users(self):
        db = _rows_db([User(email="a@example.com", display_name="A", is_active=True)])

        page = await replication.read_backfill(db, USERS)

        assert page[0]["email"] == "a@example.com"

    @pytest.mark.replication_matrix("users", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = User(email="a@example.com", display_name="Old", is_active=True)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, USERS, {"email": "a@example.com", "display_name": "New"})

        assert existing.display_name == "New"

    @pytest.mark.replication_matrix("users", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = User(email="a@example.com", display_name="A", is_active=True)
        db.scalar.return_value = existing
        payload = replication.serialize_row(USERS, existing)

        await replication.apply_upsert(db, USERS, payload)
        await replication.apply_upsert(db, USERS, payload)

        assert existing.display_name == "A"
        assert existing.is_active is True

    @pytest.mark.replication_matrix("users", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, USERS, "a@example.com")

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("users", "backfill-converges-deletion")
    async def test_a_deleted_user_does_not_survive_a_backfill(self):
        db = _keys_db([("kept@example.com",), ("gone@example.com",)])

        removed = await replication.reconcile_deletions(db, USERS, {"kept@example.com"})

        assert removed == ["gone@example.com"]


class TestRoles:
    @pytest.mark.replication_matrix("roles", "backfill-from-empty")
    async def test_backfill_carries_the_permission_axes(self):
        db = _rows_db([Role(name="sre", allow_labels={"env": "prod"}, capabilities=["run:apply"])])

        page = await replication.read_backfill(db, ROLES)

        assert page[0]["allow_labels"] == {"env": "prod"}
        assert page[0]["capabilities"] == ["run:apply"]

    @pytest.mark.replication_matrix("roles", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = Role(name="sre", description="old")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, ROLES, {"name": "sre", "description": "new"})

        assert existing.description == "new"

    @pytest.mark.replication_matrix("roles", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = Role(name="sre", allow_labels={"env": "prod"}, capabilities=["run:apply"])
        db.scalar.return_value = existing
        payload = replication.serialize_row(ROLES, existing)

        await replication.apply_upsert(db, ROLES, payload)
        await replication.apply_upsert(db, ROLES, payload)

        assert existing.allow_labels == {"env": "prod"}
        assert existing.capabilities == ["run:apply"]

    @pytest.mark.replication_matrix("roles", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, ROLES, "sre")

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("roles", "backfill-converges-deletion")
    async def test_a_deleted_role_does_not_survive_a_backfill(self):
        """A role that grants permissions must not outlive its deletion — that
        is a privilege the operator believes they removed."""
        db = _keys_db([("sre",), ("deleted-role",)])

        removed = await replication.reconcile_deletions(db, ROLES, {"sre"})

        assert removed == ["deleted-role"]


class TestRoleAssignments:
    """Three-column key: (provider_name, email, role_name).

    One person may hold several roles under one (provider, email), so the
    role name is part of the identity, not a mutable attribute of it. A
    two-column key collapsed the siblings onto one entity_id and a promoted
    node granted whichever one it happened to read.
    """

    @pytest.mark.replication_matrix("role_assignments", "backfill-from-empty")
    async def test_backfill_serializes_assignments(self):
        db = _rows_db([RoleAssignment(provider_name="local", email="a@x.com", role_name="sre")])

        page = await replication.read_backfill(db, ASSIGNMENTS)

        assert (page[0]["provider_name"], page[0]["email"]) == ("local", "a@x.com")

    @pytest.mark.replication_matrix("role_assignments", "delta-apply")
    async def test_a_second_role_for_one_person_is_a_new_row(self):
        """The same person gaining a second role must INSERT, not overwrite.

        role_name is part of the key, so `viewer` and `sre` are two distinct
        entities. Treating it as a mutable attribute is what silently turned
        two grants into one.
        """
        db = AsyncMock()
        db.scalar.return_value = None  # no row for (local, a@x.com, sre) yet

        await replication.apply_upsert(
            db,
            ASSIGNMENTS,
            {"provider_name": "local", "email": "a@x.com", "role_name": "sre"},
        )

        db.add.assert_called_once()
        added = db.add.call_args.args[0]
        assert (added.provider_name, added.email, added.role_name) == ("local", "a@x.com", "sre")

    @pytest.mark.replication_matrix("role_assignments", "delta-apply")
    async def test_each_role_gets_its_own_entity_id(self):
        """Two grants for one person must not encode to the same id — that is
        what made the read return an arbitrary one and the backfill cursor page
        straight past the sibling."""
        viewer = replication.encode_entity_id(ASSIGNMENTS, ["local", "a@x.com", "viewer"])
        sre = replication.encode_entity_id(ASSIGNMENTS, ["local", "a@x.com", "sre"])

        assert viewer != sre

    @pytest.mark.replication_matrix("role_assignments", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = RoleAssignment(provider_name="local", email="a@x.com", role_name="sre")
        db.scalar.return_value = existing
        payload = replication.serialize_row(ASSIGNMENTS, existing)

        await replication.apply_upsert(db, ASSIGNMENTS, payload)
        await replication.apply_upsert(db, ASSIGNMENTS, payload)

        assert existing.role_name == "sre"

    @pytest.mark.replication_matrix("role_assignments", "delete")
    async def test_delete_applies_on_the_full_key(self):
        db = AsyncMock()
        entity_id = replication.encode_entity_id(ASSIGNMENTS, ["local", "a@x.com", "sre"])

        await replication.apply_delete(db, ASSIGNMENTS, entity_id)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("role_assignments", "backfill-converges-deletion")
    async def test_a_revoked_assignment_does_not_survive_a_backfill(self):
        """Removing someone's role is an access revocation. It must converge
        through the recovery path, not just the delta path."""
        kept = replication.encode_entity_id(ASSIGNMENTS, ["local", "who@x.com", "viewer"])
        # Same person, two roles: one kept, one revoked. With a short key both
        # rows collapsed to a single id and the revocation could not be seen.
        db = _keys_db([("local", "who@x.com", "viewer"), ("local", "who@x.com", "sre")])

        removed = await replication.reconcile_deletions(db, ASSIGNMENTS, {kept})

        assert removed == [replication.encode_entity_id(ASSIGNMENTS, ["local", "who@x.com", "sre"])]


class TestPlatformRoleAssignments:
    """Three-column key: (provider_name, email, role_name) — this is what
    grants `admin`, so a promoted node without it has no administrators."""

    @pytest.mark.replication_matrix("platform_role_assignments", "backfill-from-empty")
    async def test_backfill_serializes_platform_grants(self):
        db = _rows_db(
            [PlatformRoleAssignment(provider_name="local", email="a@x.com", role_name="admin")]
        )

        page = await replication.read_backfill(db, PLATFORM)

        assert page[0]["role_name"] == "admin"

    @pytest.mark.replication_matrix("platform_role_assignments", "delta-apply")
    async def test_a_new_admin_grant_applies(self):
        db = AsyncMock()
        db.scalar.return_value = None

        await replication.apply_upsert(
            db, PLATFORM, {"provider_name": "local", "email": "a@x.com", "role_name": "admin"}
        )

        added = db.add.call_args[0][0]
        assert (added.email, added.role_name) == ("a@x.com", "admin")

    @pytest.mark.replication_matrix("platform_role_assignments", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = PlatformRoleAssignment(provider_name="local", email="a@x.com", role_name="admin")
        db.scalar.return_value = existing
        payload = replication.serialize_row(PLATFORM, existing)

        await replication.apply_upsert(db, PLATFORM, payload)
        await replication.apply_upsert(db, PLATFORM, payload)

        assert existing.role_name == "admin"
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("platform_role_assignments", "delete")
    async def test_delete_applies_on_the_full_key(self):
        db = AsyncMock()
        entity_id = replication.encode_entity_id(PLATFORM, ["local", "a@x.com", "admin"])

        await replication.apply_delete(db, PLATFORM, entity_id)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("platform_role_assignments", "backfill-converges-deletion")
    async def test_a_removed_admin_grant_does_not_survive_a_backfill(self):
        """Someone demoted from admin must not still be an admin on the
        promoted node."""
        kept = replication.encode_entity_id(PLATFORM, ["local", "a@x.com", "audit"])
        db = _keys_db([("local", "a@x.com", "audit"), ("local", "a@x.com", "admin")])

        removed = await replication.reconcile_deletions(db, PLATFORM, {kept})

        assert removed == [replication.encode_entity_id(PLATFORM, ["local", "a@x.com", "admin"])]


class TestAPITokens:
    """The class #1115 was really about."""

    @pytest.mark.replication_matrix("api_tokens", "backfill-from-empty")
    async def test_backfill_carries_the_hash_and_scope(self):
        db = _rows_db([_token(pinned_roles=["sre"])])

        page = await replication.read_backfill(db, TOKENS)

        assert page[0]["token_hash"] == "h" * 64
        assert page[0]["pinned_roles"] == ["sre"]

    @pytest.mark.replication_matrix("api_tokens", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = _token(description="old")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, TOKENS, {"id": "at-1", "description": "rotated"})

        assert existing.description == "rotated"

    @pytest.mark.replication_matrix("api_tokens", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _token(pinned_roles=["sre"])
        db.scalar.return_value = existing
        payload = replication.serialize_row(TOKENS, existing)

        await replication.apply_upsert(db, TOKENS, payload)
        await replication.apply_upsert(db, TOKENS, payload)

        assert existing.pinned_roles == ["sre"]
        assert existing.token_hash == "h" * 64

    @pytest.mark.replication_matrix("api_tokens", "delete")
    async def test_revocation_propagates_through_the_delta_path(self):
        db = AsyncMock()

        await replication.apply_delete(db, TOKENS, "at-1")

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("api_tokens", "backfill-converges-deletion")
    async def test_a_revoked_token_does_not_survive_a_backfill(self):
        """The motivating case for #1115, on the class it motivated: revoked
        while this node was too far behind to see the delete event. Without
        reconciliation the token works again at the failover."""
        db = _keys_db([("at-live",), ("at-revoked",)])

        removed = await replication.reconcile_deletions(db, TOKENS, {"at-live"})

        assert removed == ["at-revoked"]


class TestOrdering:
    """Registration order is dependency order — backfill walks it in order."""

    def test_identity_precedes_the_things_that_reference_it(self):
        names = list(replication.registered())

        assert names.index("users") < names.index("role_assignments")
        assert names.index("roles") < names.index("role_assignments")
        assert names.index("users") < names.index("api_tokens"), "api_tokens.bound_to names a user"
