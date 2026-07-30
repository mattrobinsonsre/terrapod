"""Replication of workspaces and their two junction tables (#1136).

The class the previous three slices existed to reach. Nothing is excluded from
it, which is the considered answer rather than the lazy one: several columns look
like node-local operational state and are not, and getting any of them wrong is
worse than not replicating the workspace at all.

The one this file spends most of its assertions on is `vcs_last_commit_sha`. It
is the poller cursor, and a promoted node that has not seen it treats every
tracked branch as changed — queueing a plan **and apply** on every VCS-connected
workspace at once. That is a fleet-wide event caused by the failover itself,
which is the opposite of what a warm standby is for.

`workspace_agent_pools` is here for a separate reason: it is the first replicated
class edited as a *collection*, and the outbox hook deliberately ignores
collection changes on the parent. `TestPoolMembershipReplicatesThroughTheJunction`
pins why that is still correct.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import ModuleWorkspaceLink, Workspace, WorkspaceAgentPool
from terrapod.services import replication, replication_registry

WORKSPACES = replication_registry.WORKSPACES
POOL_LINKS = replication_registry.WORKSPACE_AGENT_POOLS
MODULE_LINKS = replication_registry.MODULE_WORKSPACE_LINKS

WS_ID = "44444444-4444-4444-4444-444444444444"
OTHER_WS_ID = "55555555-5555-5555-5555-555555555555"
POOL_ID = "66666666-6666-6666-6666-666666666666"
OTHER_POOL_ID = "77777777-7777-7777-7777-777777777777"
MODULE_ID = "22222222-2222-2222-2222-222222222222"
LINK_ID = "88888888-8888-8888-8888-888888888888"
CONN_ID = "11111111-1111-1111-1111-111111111111"

HEAD = "a" * 40


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


def _ws(ws_id=WS_ID, **kw):
    now = datetime.now(UTC)
    base = {
        "id": ws_id,
        "name": "prod-network",
        "execution_mode": "agent",
        "execution_backend": "tofu",
        "terraform_version": "1.12",
        "vcs_connection_id": CONN_ID,
        "vcs_repo_url": "https://github.com/example/infra",
        "vcs_branch": "main",
        "vcs_last_commit_sha": HEAD,
        "labels": {"team": "platform"},
        "owner_email": "owner@example.com",
        "created_at": now,
        "updated_at": now,
    }
    base.update(kw)
    return Workspace(**base)


class TestThePollerCursor:
    """The omission that would turn a failover into a fleet-wide apply."""

    @pytest.mark.replication_matrix("workspaces", "delta-apply")
    async def test_the_tracked_head_reaches_the_peer(self):
        db = AsyncMock()
        existing = _ws(vcs_last_commit_sha="b" * 40)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, WORKSPACES, {"id": WS_ID, "vcs_last_commit_sha": HEAD})

        assert existing.vcs_last_commit_sha == HEAD

    def test_the_cursor_is_not_excluded(self):
        """Asserted structurally as well as behaviourally: an `exclude` entry
        added here later would reintroduce the fleet-wide-apply failure while
        every other test in this file still passed."""
        payload = replication.serialize_row(WORKSPACES, _ws())

        assert payload["vcs_last_commit_sha"] == HEAD
        assert "vcs_last_commit_sha" not in WORKSPACES.exclude

    def test_nothing_at_all_is_excluded_from_a_workspace(self):
        """Every column was considered individually and all of them carry. If a
        future change needs an exclusion it should have to argue with this
        test rather than slip past it."""
        assert WORKSPACES.exclude == frozenset()


class TestStateSafetyFlags:
    """Three flags where losing the value is worse than carrying a stale one."""

    async def test_a_held_lock_survives(self):
        """Dropping a held state lock at promotion lets two writers collide.
        Carrying a stale one costs a manual unlock — so it fails closed."""
        db = AsyncMock()
        existing = _ws(locked=False, lock_id=None)
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, WORKSPACES, {"id": WS_ID, "locked": True, "lock_id": "lock-123"}
        )

        assert existing.locked is True
        assert existing.lock_id == "lock-123"

    async def test_state_divergence_survives(self):
        """Set when an apply succeeded but its state upload did not. A node that
        loses it believes state is good when it is not."""
        db = AsyncMock()
        existing = _ws(state_diverged=False)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, WORKSPACES, {"id": WS_ID, "state_diverged": True})

        assert existing.state_diverged is True

    async def test_a_workspace_awaiting_a_human_decision_stays_that_way(self):
        """`pending_deletion` means an operator has to decide. Resetting it to
        `active` at promotion silently discards that decision, and the workspace
        rejoins normal operation as though nothing had happened."""
        db = AsyncMock()
        existing = _ws(lifecycle_state="active")
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db,
            WORKSPACES,
            {
                "id": WS_ID,
                "lifecycle_state": "pending_deletion",
                "lifecycle_reason": "origin directory removed",
            },
        )

        assert existing.lifecycle_state == "pending_deletion"
        assert existing.lifecycle_reason == "origin directory removed"


class TestWorkspaces:
    @pytest.mark.replication_matrix("workspaces", "backfill-from-empty")
    async def test_backfill_carries_settings_rbac_and_vcs_wiring(self):
        db = _rows_db([_ws()])

        page = await replication.read_backfill(db, WORKSPACES)

        assert page[0]["name"] == "prod-network"
        assert page[0]["execution_mode"] == "agent"
        assert page[0]["labels"] == {"team": "platform"}
        assert page[0]["owner_email"] == "owner@example.com"
        assert page[0]["vcs_connection_id"] == CONN_ID

    @pytest.mark.replication_matrix("workspaces", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _ws()
        db.scalar.return_value = existing
        payload = replication.serialize_row(WORKSPACES, existing)

        await replication.apply_upsert(db, WORKSPACES, payload)
        await replication.apply_upsert(db, WORKSPACES, payload)

        assert existing.name == "prod-network"
        assert existing.vcs_last_commit_sha == HEAD
        assert existing.labels == {"team": "platform"}

    @pytest.mark.replication_matrix("workspaces", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, WORKSPACES, WS_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("workspaces", "backfill-converges-deletion")
    async def test_a_deleted_workspace_does_not_survive_a_backfill(self):
        db = _keys_db([(WS_ID,), (OTHER_WS_ID,)])

        removed = await replication.reconcile_deletions(db, WORKSPACES, {WS_ID})

        assert removed == [OTHER_WS_ID]

    async def test_the_catalog_id_is_carried_because_rbac_depends_on_it(self):
        """`workspace_rbac_service` clamps every non-platform-admin grant to read
        on a catalog-managed workspace. Losing the id would WIDEN access at the
        moment of a failover, which is why it could never have been deferred."""
        db = AsyncMock()
        existing = _ws(catalog_item_id=None)
        db.scalar.return_value = existing

        await replication.apply_upsert(db, WORKSPACES, {"id": WS_ID, "catalog_item_id": LINK_ID})

        assert str(existing.catalog_item_id) == LINK_ID

    def test_the_drift_run_link_is_carried_and_may_dangle(self):
        """Runs are a later phase, and this column is deliberately not a foreign
        key so artifact retention cannot cascade into workspace deletion. So it
        can point at a run the peer does not have — a dead click-through the UI
        already handles, which beats a special case here."""
        from sqlalchemy import inspect as sa_inspect

        column = sa_inspect(Workspace).local_table.c["drift_latest_run_id"]

        assert not column.foreign_keys, (
            "drift_latest_run_id gained a foreign key — replicating a workspace "
            "would now require replicating runs first"
        )
        payload = replication.serialize_row(WORKSPACES, _ws(drift_latest_run_id=LINK_ID))
        assert payload["drift_latest_run_id"] == LINK_ID


class TestPoolMembershipReplicatesThroughTheJunction:
    """The outbox hook asks `is_modified(..., include_collections=False)`, so
    editing `workspace.agent_pool_links` does not mark the workspace row dirty.
    That is correct — but only because the junction rows are a registered class
    of their own, so an add lands in `session.new` and a removal in
    `session.deleted`. If this were wrong, pool membership edits would silently
    never replicate and a promoted node would dispatch runs to the wrong pools.
    """

    def test_the_junction_is_registered_in_its_own_right(self):
        assert "workspace_agent_pools" in replication.registered()

    def test_it_is_keyed_by_both_sides(self):
        assert POOL_LINKS.pk_attrs == ("workspace_id", "agent_pool_id")

    def test_the_hook_still_ignores_collection_changes_on_the_parent(self):
        """Pinned deliberately. Flipping this to include collections would make
        every membership edit emit a redundant workspace event as well — and,
        worse, would suggest the junction no longer needs to be registered."""
        import inspect as py_inspect

        source = py_inspect.getsource(replication.install_outbox_hooks)

        assert "include_collections=False" in source

    @pytest.mark.replication_matrix("workspace_agent_pools", "backfill-from-empty")
    async def test_backfill_carries_the_membership_and_its_order(self):
        db = _rows_db([WorkspaceAgentPool(workspace_id=WS_ID, agent_pool_id=POOL_ID, ordinal=1)])

        page = await replication.read_backfill(db, POOL_LINKS)

        assert page[0]["workspace_id"] == WS_ID
        assert page[0]["agent_pool_id"] == POOL_ID
        assert page[0]["ordinal"] == 1

    @pytest.mark.replication_matrix("workspace_agent_pools", "delta-apply")
    async def test_reordering_applies(self):
        """`ordinal` is display order only — it carries no dispatch meaning — but
        it is what the API echoes back, so a peer that disagrees shows the
        operator a different set than they typed."""
        db = AsyncMock()
        existing = WorkspaceAgentPool(workspace_id=WS_ID, agent_pool_id=POOL_ID, ordinal=0)
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db,
            POOL_LINKS,
            {"workspace_id": WS_ID, "agent_pool_id": POOL_ID, "ordinal": 2},
        )

        assert existing.ordinal == 2

    @pytest.mark.replication_matrix("workspace_agent_pools", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = WorkspaceAgentPool(workspace_id=WS_ID, agent_pool_id=POOL_ID, ordinal=1)
        db.scalar.return_value = existing
        payload = replication.serialize_row(POOL_LINKS, existing)

        await replication.apply_upsert(db, POOL_LINKS, payload)
        await replication.apply_upsert(db, POOL_LINKS, payload)

        assert existing.ordinal == 1

    @pytest.mark.replication_matrix("workspace_agent_pools", "delete")
    async def test_removing_a_pool_from_the_set_applies(self):
        db = AsyncMock()
        entity_id = replication.encode_entity_id(POOL_LINKS, [WS_ID, POOL_ID])

        await replication.apply_delete(db, POOL_LINKS, entity_id)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("workspace_agent_pools", "backfill-converges-deletion")
    async def test_a_removed_pool_does_not_come_back_through_a_backfill(self):
        """A pool that reappears is a pool that can claim runs the operator
        deliberately stopped sending it."""
        db = _keys_db([(WS_ID, POOL_ID), (WS_ID, OTHER_POOL_ID)])
        kept = replication.encode_entity_id(POOL_LINKS, [WS_ID, POOL_ID])

        removed = await replication.reconcile_deletions(db, POOL_LINKS, {kept})

        assert removed == [replication.encode_entity_id(POOL_LINKS, [WS_ID, OTHER_POOL_ID])]


class TestModuleWorkspaceLinks:
    @pytest.mark.replication_matrix("module_workspace_links", "backfill-from-empty")
    async def test_backfill_carries_both_sides(self):
        db = _rows_db(
            [
                ModuleWorkspaceLink(
                    id=LINK_ID,
                    module_id=MODULE_ID,
                    workspace_id=WS_ID,
                    created_at=datetime.now(UTC),
                    created_by="admin",
                )
            ]
        )

        page = await replication.read_backfill(db, MODULE_LINKS)

        assert page[0]["module_id"] == MODULE_ID
        assert page[0]["workspace_id"] == WS_ID

    @pytest.mark.replication_matrix("module_workspace_links", "delta-apply")
    async def test_delta_applies(self):
        db = AsyncMock()
        existing = ModuleWorkspaceLink(
            id=LINK_ID, module_id=MODULE_ID, workspace_id=WS_ID, created_by="admin"
        )
        db.scalar.return_value = existing

        await replication.apply_upsert(
            db, MODULE_LINKS, {"id": LINK_ID, "workspace_id": OTHER_WS_ID}
        )

        assert str(existing.workspace_id) == OTHER_WS_ID

    @pytest.mark.replication_matrix("module_workspace_links", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = ModuleWorkspaceLink(
            id=LINK_ID,
            module_id=MODULE_ID,
            workspace_id=WS_ID,
            created_at=datetime.now(UTC),
            created_by="admin",
        )
        db.scalar.return_value = existing
        payload = replication.serialize_row(MODULE_LINKS, existing)

        await replication.apply_upsert(db, MODULE_LINKS, payload)
        await replication.apply_upsert(db, MODULE_LINKS, payload)

        assert str(existing.workspace_id) == WS_ID
        assert existing.created_by == "admin"

    @pytest.mark.replication_matrix("module_workspace_links", "delete")
    async def test_delete_applies(self):
        db = AsyncMock()

        await replication.apply_delete(db, MODULE_LINKS, LINK_ID)

        db.execute.assert_awaited()

    @pytest.mark.replication_matrix("module_workspace_links", "backfill-converges-deletion")
    async def test_an_unlinked_workspace_stays_unlinked(self):
        """Otherwise a promoted node resumes firing speculative plans on a
        workspace somebody deliberately detached from the module."""
        db = _keys_db([(LINK_ID,), (WS_ID,)])

        removed = await replication.reconcile_deletions(db, MODULE_LINKS, {LINK_ID})

        assert removed == [WS_ID]


class TestOrdering:
    """The #1134 gate checks foreign keys generically. These are the specific
    orderings this slice depends on, stated once."""

    def test_workspaces_come_after_everything_they_point_at(self):
        order = list(replication.registered())

        for earlier in ("vcs_connections", "autodiscovery_rules", "catalog_items"):
            assert order.index(earlier) < order.index("workspaces"), (
                f"{earlier} must precede workspaces"
            )

    def test_the_junctions_come_after_both_of_their_sides(self):
        order = list(replication.registered())

        assert order.index("workspaces") < order.index("workspace_agent_pools")
        assert order.index("agent_pools") < order.index("workspace_agent_pools")
        assert order.index("workspaces") < order.index("module_workspace_links")
        assert order.index("registry_modules") < order.index("module_workspace_links")
