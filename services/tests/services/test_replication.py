"""The replication framework (#960 phase 3, #1110).

Two things get the most attention here, because they are the two that fail
quietly rather than loudly:

**Monotonic fields.** `use_count` is a budget both nodes spend, and a stale copy
winning on timestamp would hand a spent join token its uses back — extra
credentials, not merely lost information. There is no user-visible symptom until
someone joins with a token that should have been exhausted.

**Stale-cursor detection.** A follower whose cursor has fallen off the end of the
retained window must be told, or it replays an innocent-looking empty page and
believes it is in sync while silently missing every purged change.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.db.models import (
    AgentPool,
    AgentPoolToken,
    PlatformRoleAssignment,
    ReplicationEvent,
)
from terrapod.services import replication, replication_registry

POOLS = replication_registry.AGENT_POOLS
TOKENS = replication_registry.AGENT_POOL_TOKENS


class TestRegistry:
    def test_registration_order_is_dependency_order(self):
        """Backfill walks this in order — a token cannot land before its pool."""
        names = list(replication.registered())
        assert names.index("agent_pools") < names.index("agent_pool_tokens")

    def test_unknown_class_resolves_to_none(self):
        assert replication.get("not_a_class") is None

    def test_the_counter_class_declares_its_merge_rule(self):
        """If this ever silently reverts to blanket LWW, a spent token gets its
        budget back — so the declaration itself is worth pinning."""
        assert "use_count" in TOKENS.monotonic_fields
        assert "is_revoked" in TOKENS.one_way_true_fields


class TestSerialisation:
    def test_round_trips_a_row(self):
        pool_id = uuid.uuid4()
        pool = AgentPool(
            id=pool_id,
            name="aws-prod",
            description="",
            labels={"env": "prod"},
            owner_email="a@example.com",
            created_at=datetime.now(UTC),
        )

        payload = replication.serialize_row(POOLS, pool)

        assert payload["id"] == str(pool_id)
        assert payload["name"] == "aws-prod"
        assert payload["labels"] == {"env": "prod"}

    def test_timestamps_use_the_house_rfc3339_z_form(self):
        pool = AgentPool(id=uuid.uuid4(), name="p", created_at=datetime(2026, 1, 1, tzinfo=UTC))

        payload = replication.serialize_row(POOLS, pool)

        assert payload["created_at"] == "2026-01-01T00:00:00Z"

    def test_coerce_restores_native_types(self):
        pool_id = uuid.uuid4()
        values = replication._coerce(
            POOLS, {"id": str(pool_id), "name": "p", "created_at": "2026-01-01T00:00:00Z"}
        )

        assert values["id"] == pool_id
        assert isinstance(values["created_at"], datetime)

    def test_unknown_wire_keys_are_ignored(self):
        """An older node must survive a newer peer sending a column it lacks."""
        values = replication._coerce(POOLS, {"id": str(uuid.uuid4()), "invented_column": 1})

        assert "invented_column" not in values


class TestMergeRules:
    """The field-level exceptions that blanket last-write-wins gets wrong."""

    @pytest.mark.replication_matrix("agent_pool_tokens", "monotonic-never-regresses")
    def test_counter_never_decreases(self):
        existing = SimpleNamespace(use_count=7, is_revoked=False)

        merged = replication._merge(TOKENS, existing, {"use_count": 5})

        assert merged["use_count"] == 7, "a stale count would refund a spent token"

    def test_counter_still_advances(self):
        existing = SimpleNamespace(use_count=7, is_revoked=False)

        merged = replication._merge(TOKENS, existing, {"use_count": 9})

        assert merged["use_count"] == 9

    @pytest.mark.replication_matrix("agent_pool_tokens", "one-way-never-reverts")
    def test_revocation_is_one_way(self):
        existing = SimpleNamespace(use_count=0, is_revoked=True)

        merged = replication._merge(TOKENS, existing, {"is_revoked": False})

        assert merged["is_revoked"] is True, "a revoked token must never become usable again"

    def test_revocation_still_propagates(self):
        existing = SimpleNamespace(use_count=0, is_revoked=False)

        merged = replication._merge(TOKENS, existing, {"is_revoked": True})

        assert merged["is_revoked"] is True

    def test_ordinary_fields_take_the_incoming_value(self):
        existing = SimpleNamespace(name="old", use_count=0, is_revoked=False)

        merged = replication._merge(POOLS, existing, {"name": "new"})

        assert merged["name"] == "new"

    def test_null_counter_is_treated_as_zero(self):
        """A peer that omits or nulls the field must not blank a real count."""
        existing = SimpleNamespace(use_count=4, is_revoked=False)

        merged = replication._merge(TOKENS, existing, {"use_count": None})

        assert merged["use_count"] == 4


class TestEventReading:
    async def _db_with(self, oldest, rows):
        db = AsyncMock()
        db.scalar.return_value = oldest
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        db.execute.return_value = result
        return db

    def _event(self, event_id):
        return ReplicationEvent(
            id=event_id,
            entity_class="agent_pools",
            entity_id=str(uuid.uuid4()),
            op="upsert",
            occurred_at=datetime.now(UTC),
            origin_node="node-a",
        )

    async def test_returns_events_and_advances_the_cursor(self):
        db = await self._db_with(oldest=1, rows=[self._event(4), self._event(5)])

        page = await replication.read_events(db, after=3)

        assert len(page.events) == 2
        assert page.cursor == 5
        assert page.stale_cursor is False

    async def test_empty_page_holds_the_cursor(self):
        db = await self._db_with(oldest=1, rows=[])

        page = await replication.read_events(db, after=9)

        assert page.cursor == 9

    async def test_detects_a_cursor_that_fell_off_the_window(self):
        """Events 4..40 were purged. Replaying would skip them in silence, so
        the caller has to be told to backfill instead."""
        db = await self._db_with(oldest=41, rows=[self._event(41)])

        page = await replication.read_events(db, after=3)

        assert page.stale_cursor is True

    async def test_a_contiguous_window_is_not_stale(self):
        db = await self._db_with(oldest=4, rows=[self._event(4)])

        page = await replication.read_events(db, after=3)

        assert page.stale_cursor is False

    async def test_a_fresh_follower_is_not_stale(self):
        """Cursor 0 means "never synced", which is a backfill either way — but
        it must not be reported as a *gap*, which would look like data loss."""
        db = await self._db_with(oldest=500, rows=[self._event(500)])

        page = await replication.read_events(db, after=0)

        assert page.stale_cursor is False

    async def test_an_empty_outbox_is_not_stale(self):
        db = await self._db_with(oldest=None, rows=[])

        page = await replication.read_events(db, after=7)

        assert page.stale_cursor is False


class TestApply:
    def _token(self, **kw):
        base = {
            "id": uuid.uuid4(),
            "pool_id": uuid.uuid4(),
            "token_hash": "h" * 64,
            "description": "",
            "max_uses": 10,
            "use_count": 0,
            "is_revoked": False,
            "created_at": datetime.now(UTC),
            "created_by": "admin",
        }
        base.update(kw)
        return AgentPoolToken(**base)

    async def test_insert_preserves_the_peers_identity(self):
        """A replicated row keeps its own id — a new one would make the two
        nodes disagree about which row is which forever."""
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        pool_id = uuid.uuid4()

        await replication.apply_upsert(
            db, POOLS, {"id": str(pool_id), "name": "p", "created_at": "2026-01-01T00:00:00Z"}
        )

        added = db.add.call_args[0][0]
        assert added.id == pool_id

    @pytest.mark.replication_matrix("agent_pools", "delta-apply")
    async def test_update_applies_onto_the_existing_row(self):
        db = AsyncMock()
        existing = AgentPool(id=uuid.uuid4(), name="old")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, POOLS, {"id": str(existing.id), "name": "new"})

        assert existing.name == "new"
        db.add.assert_not_called()

    @pytest.mark.replication_matrix("agent_pools", "idempotent-reapply")
    async def test_reapplying_the_same_row_changes_nothing(self):
        db = AsyncMock()
        existing = AgentPool(id=uuid.uuid4(), name="p", labels={"env": "prod"})
        db.scalar.return_value = existing
        payload = replication.serialize_row(POOLS, existing)

        await replication.apply_upsert(db, POOLS, payload)
        await replication.apply_upsert(db, POOLS, payload)

        assert existing.name == "p"
        assert existing.labels == {"env": "prod"}

    async def test_update_honours_the_monotonic_rule(self):
        db = AsyncMock()
        existing = self._token(use_count=7)
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, TOKENS, {"id": str(existing.id), "use_count": 2, "is_revoked": False}
        )

        assert existing.use_count == 7

    async def test_a_payload_without_a_primary_key_is_ignored(self):
        db = AsyncMock()

        await replication.apply_upsert(db, POOLS, {"name": "orphan"})

        db.add.assert_not_called()

    @pytest.mark.replication_matrix("agent_pools", "delete")
    async def test_delete_removes_the_row(self):
        db = AsyncMock()

        await replication.apply_delete(db, POOLS, str(uuid.uuid4()))

        db.execute.assert_awaited()

    async def test_a_malformed_id_is_not_a_crash(self):
        """A peer sending nonsense must not wedge the stream for every row."""
        db = AsyncMock()

        await replication.apply_delete(db, POOLS, "not-a-uuid")

        db.execute.assert_not_awaited()


class TestRetention:
    async def test_purges_beyond_the_window(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(rowcount=12)

        count = await replication.purge_old_events(db, retention_days=7)

        assert count == 12
        db.commit.assert_awaited()

    async def test_purging_is_safe_because_backfill_covers_the_gap(self):
        """Documented as a test so the coupling is not lost: purging is only
        acceptable while `read_events` reports a stale cursor."""
        db = AsyncMock()
        db.scalar.return_value = 100
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute.return_value = result

        page = await replication.read_events(db, after=5)

        assert page.stale_cursor is True


class TestOutboxHooks:
    """The hook is what makes registration the whole opt-in — no write path has
    to remember to emit an event, and none can forget."""

    def test_records_inserts_updates_and_deletes(self):
        session = MagicMock()
        pool = AgentPool(id=uuid.uuid4(), name="p")
        session.new = [pool]
        session.dirty = []
        session.deleted = []

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ha = SimpleNamespace(node_name="node-a")
            replication._record(session, POOLS, pool, replication.UPSERT, "node-a")

        event = session.add.call_args[0][0]
        assert event.entity_class == "agent_pools"
        assert event.entity_id == str(pool.id)
        assert event.op == replication.UPSERT

    def test_tags_the_origin_so_the_pair_cannot_echo(self):
        session = MagicMock()
        pool = AgentPool(id=uuid.uuid4(), name="p")

        replication._record(session, POOLS, pool, replication.UPSERT, "node-a")

        assert session.add.call_args[0][0].origin_node == "node-a"

    def test_a_row_with_no_primary_key_records_nothing(self):
        session = MagicMock()

        replication._record(session, POOLS, AgentPool(name="p"), replication.UPSERT, "node-a")

        session.add.assert_not_called()


class TestBackfillPaging:
    """Ordered by primary key, not by time, so an interrupted backfill resumes
    instead of restarting a large class."""

    @pytest.mark.replication_matrix("agent_pools", "backfill-from-empty")
    async def test_returns_serialized_rows(self):
        db = AsyncMock()
        rows = [AgentPool(id=uuid.uuid4(), name="a"), AgentPool(id=uuid.uuid4(), name="b")]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        db.execute.return_value = result

        page = await replication.read_backfill(db, POOLS)

        assert [r["name"] for r in page] == ["a", "b"]

    async def test_a_malformed_resume_key_returns_nothing(self):
        db = AsyncMock()

        assert await replication.read_backfill(db, POOLS, after="not-a-uuid") == []


class TestEventPayloadPolicy:
    """Events carry no row contents, deliberately — see ReplicationEvent."""

    def test_an_event_holds_only_identity(self):
        event = ReplicationEvent(
            id=1,
            entity_class="agent_pools",
            entity_id="x",
            op="upsert",
            occurred_at=datetime.now(UTC) - timedelta(minutes=1),
            origin_node="node-a",
        )

        columns = {c.name for c in event.__table__.columns}
        assert "payload" not in columns
        assert "attributes" not in columns


class TestAgentPoolTokensMatrix:
    """The per-class matrix for join tokens (#1112).

    Tokens get their own block rather than riding the generic tests because
    they are the class with merge rules — and a class whose counter is only
    exercised through a shared helper is one whose real apply path was never
    tried.
    """

    def _token(self, **kw):
        base = {
            "id": uuid.uuid4(),
            "pool_id": uuid.uuid4(),
            "token_hash": "h" * 64,
            "description": "",
            "max_uses": 10,
            "use_count": 0,
            "is_revoked": False,
            "created_at": datetime.now(UTC),
            "created_by": "admin",
        }
        base.update(kw)
        return AgentPoolToken(**base)

    @pytest.mark.replication_matrix("agent_pool_tokens", "backfill-from-empty")
    async def test_backfill_serializes_tokens(self):
        db = AsyncMock()
        rows = [self._token(use_count=3)]
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        db.execute.return_value = result

        page = await replication.read_backfill(db, TOKENS)

        assert page[0]["use_count"] == 3
        assert page[0]["token_hash"] == "h" * 64

    @pytest.mark.replication_matrix("agent_pool_tokens", "delta-apply")
    async def test_delta_applies_onto_an_existing_token(self):
        db = AsyncMock()
        existing = self._token(description="old")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, TOKENS, {"id": str(existing.id), "description": "rotated", "use_count": 0}
        )

        assert existing.description == "rotated"

    @pytest.mark.replication_matrix("agent_pool_tokens", "idempotent-reapply")
    async def test_reapplying_a_token_changes_nothing(self):
        db = AsyncMock()
        existing = self._token(use_count=4)
        db.scalar.return_value = existing
        payload = replication.serialize_row(TOKENS, existing)

        await replication.apply_upsert(db, TOKENS, payload)
        await replication.apply_upsert(db, TOKENS, payload)

        assert existing.use_count == 4
        assert existing.is_revoked is False

    @pytest.mark.replication_matrix("agent_pool_tokens", "delete")
    async def test_a_deleted_token_is_removed(self):
        db = AsyncMock()

        await replication.apply_delete(db, TOKENS, str(uuid.uuid4()))

        db.execute.assert_awaited()


class TestRoleChangeConflict:
    """What happens when both sides touched the same row (#1112).

    This is the case a failover actually produces: A was written to before the
    cutover, B after it. There is no clever resolution here, and there should
    not be — the rules just have to be the intended ones, and stated.
    """

    @pytest.mark.replication_matrix("agent_pools", "role-change-conflict")
    async def test_settings_take_the_peers_value(self):
        """Plain settings are last-writer-wins by arrival, and that is correct:
        only the leader originates change, so the value arriving from the peer
        is the one the leader has."""
        db = AsyncMock()
        existing = AgentPool(id=uuid.uuid4(), name="edited-locally", description="local")
        db.scalar.return_value = existing

        await replication.apply_upsert(db, POOLS, {"id": str(existing.id), "name": "from-peer"})

        assert existing.name == "from-peer"

    @pytest.mark.replication_matrix("agent_pool_tokens", "role-change-conflict")
    async def test_both_sides_spending_the_budget_converges_upward(self):
        """The real shape of a shared listener fleet across a failover: joins
        landed on A before the cutover and on B after it. Neither count is
        'right' — but the token must never end up with MORE budget than the two
        of them have already spent between them."""
        db = AsyncMock()
        local = AgentPoolToken(
            id=uuid.uuid4(),
            pool_id=uuid.uuid4(),
            token_hash="h" * 64,
            max_uses=10,
            use_count=6,  # spent here after the cutover
            is_revoked=False,
            created_by="admin",
        )
        db.scalar.return_value = local

        # The peer reports its own, older count.
        await replication.apply_upsert(
            db, TOKENS, {"id": str(local.id), "use_count": 4, "is_revoked": False}
        )

        assert local.use_count == 6, "converging downward would refund spent uses"

    @pytest.mark.replication_matrix("agent_pool_tokens", "one-way-never-reverts")
    async def test_a_revocation_on_either_side_sticks(self):
        """Revoking during an incident, on whichever node answered, must survive
        replication from a peer that had not seen it yet."""
        db = AsyncMock()
        local = AgentPoolToken(
            id=uuid.uuid4(),
            pool_id=uuid.uuid4(),
            token_hash="h" * 64,
            use_count=1,
            is_revoked=True,
            created_by="admin",
        )
        db.scalar.return_value = local

        await replication.apply_upsert(
            db, TOKENS, {"id": str(local.id), "use_count": 1, "is_revoked": False}
        )

        assert local.is_revoked is True


class TestBackfillConvergesDeletion:
    """Backfill must remove rows the peer no longer has (#1115).

    Distinct from the `delete` row above, which only exercises the delta path.
    The gap this covers is a delete that happened while the node was beyond the
    retained event window — the exact case backfill exists to recover from, and
    the one where a revoked API token would otherwise survive and work again
    after a failover.
    """

    async def _db_with_local_ids(self, ids):
        """`select(*pk_columns)` yields key tuples, not scalars — composite
        classes have more than one component per row."""
        db = AsyncMock()
        result = MagicMock()
        result.all.return_value = [(i,) for i in ids]
        db.execute.return_value = result
        return db

    @pytest.mark.replication_matrix("agent_pools", "backfill-converges-deletion")
    async def test_removes_a_row_the_peer_dropped(self):
        kept, dropped = uuid.uuid4(), uuid.uuid4()
        db = await self._db_with_local_ids([kept, dropped])

        removed = await replication.reconcile_deletions(db, POOLS, {str(kept)})

        assert removed == [str(dropped)]
        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("agent_pool_tokens", "backfill-converges-deletion")
    async def test_a_revoked_token_does_not_survive(self):
        """The motivating case: revoked on the leader while this node was too
        far behind to see the event."""
        live, revoked = uuid.uuid4(), uuid.uuid4()
        db = await self._db_with_local_ids([live, revoked])

        removed = await replication.reconcile_deletions(db, TOKENS, {str(live)})

        assert removed == [str(revoked)]

    async def test_an_in_sync_class_removes_nothing(self):
        """The overwhelmingly common case must not issue a DELETE at all."""
        a, b = uuid.uuid4(), uuid.uuid4()
        db = await self._db_with_local_ids([a, b])

        removed = await replication.reconcile_deletions(db, POOLS, {str(a), str(b)})

        assert removed == []
        # One SELECT for the local ids, and no DELETE.
        assert db.execute.await_count == 1

    async def test_the_peer_having_nothing_clears_the_class(self):
        """Legitimate — the leader really may have deleted every row — but it
        is the highest-blast-radius case, so it is pinned deliberately."""
        db = await self._db_with_local_ids([uuid.uuid4(), uuid.uuid4()])

        removed = await replication.reconcile_deletions(db, POOLS, set())

        assert len(removed) == 2


class TestCompositePrimaryKeys:
    """Junction tables key on more than one column (#1119).

    Role assignments are keyed by (provider_name, email); a promoted node with
    users and roles but no mapping between them has nobody with any
    permissions, so these classes are not deferrable.
    """

    COMPOSITE = replication.ReplicatedClass(
        name="_test_composite",
        model=PlatformRoleAssignment,
        pk_attrs=("provider_name", "email", "role_name"),
    )

    def test_a_single_key_class_keeps_its_plain_id(self):
        """Unchanged on the wire and in the outbox — nothing already written
        needs re-encoding."""
        row_id = uuid.uuid4()

        assert replication.encode_entity_id(POOLS, [row_id]) == str(row_id)

    def test_a_composite_id_round_trips(self):
        parts = ["local", "a@example.com", "admin"]

        encoded = replication.encode_entity_id(self.COMPOSITE, parts)
        assert replication.decode_entity_id(self.COMPOSITE, encoded) == parts

    def test_components_that_would_collide_stay_distinct(self):
        """The reason the encoding is not naive concatenation: a separator can
        appear inside a component, and two distinct assignments aliasing to one
        id means a silent overwrite."""
        a = replication.encode_entity_id(self.COMPOSITE, ["local", "x:y", "admin"])
        b = replication.encode_entity_id(self.COMPOSITE, ["local:x", "y", "admin"])

        assert a != b
        assert replication.decode_entity_id(self.COMPOSITE, a) == ["local", "x:y", "admin"]
        assert replication.decode_entity_id(self.COMPOSITE, b) == ["local:x", "y", "admin"]

    def test_an_id_is_url_path_safe(self):
        """It travels as a path segment on the entity endpoint."""
        encoded = replication.encode_entity_id(
            self.COMPOSITE, ["local", "a+b/c@example.com", "ad min"]
        )

        assert all(ch.isalnum() or ch in "-_" for ch in encoded), encoded

    def test_a_malformed_composite_id_decodes_to_none(self):
        """A peer sending nonsense must not wedge the stream."""
        assert replication.decode_entity_id(self.COMPOSITE, "not-base64!!") is None

    def test_the_wrong_component_count_decodes_to_none(self):
        """A skewed peer with a different key shape must be refused, not
        applied against a mismatched filter."""
        two_parts = replication.encode_entity_id(
            replication.ReplicatedClass(
                name="_two", model=PlatformRoleAssignment, pk_attrs=("provider_name", "email")
            ),
            ["local", "a@example.com"],
        )

        assert replication.decode_entity_id(self.COMPOSITE, two_parts) is None

    def test_the_outbox_records_the_full_key(self):
        session = MagicMock()
        row = PlatformRoleAssignment(
            provider_name="local", email="a@example.com", role_name="admin"
        )

        replication._record(session, self.COMPOSITE, row, replication.UPSERT, "node-a")

        event = session.add.call_args[0][0]
        assert replication.decode_entity_id(self.COMPOSITE, event.entity_id) == [
            "local",
            "a@example.com",
            "admin",
        ]

    def test_a_row_missing_a_component_records_nothing(self):
        session = MagicMock()
        row = PlatformRoleAssignment(provider_name="local", email=None, role_name="admin")

        replication._record(session, self.COMPOSITE, row, replication.UPSERT, "node-a")

        session.add.assert_not_called()

    async def test_upsert_matches_on_every_component(self):
        """Filtering on only the first would update the wrong assignment."""
        db = AsyncMock()
        db.scalar.return_value = None

        await replication.apply_upsert(
            db,
            self.COMPOSITE,
            {"provider_name": "local", "email": "a@example.com", "role_name": "admin"},
        )

        added = db.add.call_args[0][0]
        assert (added.provider_name, added.email, added.role_name) == (
            "local",
            "a@example.com",
            "admin",
        )

    async def test_upsert_skips_a_payload_missing_a_component(self):
        db = AsyncMock()

        await replication.apply_upsert(
            db, self.COMPOSITE, {"provider_name": "local", "role_name": "admin"}
        )

        db.add.assert_not_called()
