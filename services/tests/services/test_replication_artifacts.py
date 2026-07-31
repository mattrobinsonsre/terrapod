"""The artifact plane: the rows that name objects in the store (#1175).

Everything else replicated describes how the estate is CONFIGURED. These two
describe what it has actually DONE, and they were missing while the object store
was unreplicated — which was coherent at the time, because a row naming a blob
this node does not hold is a promise it cannot keep.

#1114 copies the blobs, and that inverts the argument. A live pair showed the
consequence directly: the state file present on the follower, the workspace
present, and zero state versions pointing at it.

That is the worse of the two failure directions. An absent state version reads
as "this workspace has never run", so a promoted node does not error — it plans
the whole estate as a first-time create, and the operator sees a plan rather
than a fault.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import ConfigurationVersion, StateVersion
from terrapod.services import replication, replication_registry

STATE_VERSIONS = replication_registry.STATE_VERSIONS
CONFIG_VERSIONS = replication_registry.CONFIGURATION_VERSIONS

WS_ID = uuid.uuid4()


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


def _state(**kw):
    base = {
        "id": uuid.uuid4(),
        "workspace_id": WS_ID,
        "serial": 1,
        "lineage": str(uuid.uuid4()),
        "md5": "0" * 32,
        "sha256": "a" * 64,
        "state_size": 42,
        "created_by": "admin",
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return StateVersion(**base)


def _config(**kw):
    base = {
        "id": uuid.uuid4(),
        "workspace_id": WS_ID,
        "source": "tfe-api",
        "speculative": False,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return ConfigurationVersion(**base)


class TestStateVersionsCarryTheHistory:
    @pytest.mark.replication_matrix("state_versions", "backfill-from-empty")
    async def test_backfill_carries_every_version_not_only_the_latest(self):
        """Rollback is a shipped feature. A node holding only HEAD has lost
        rollback depth and looks perfectly healthy doing it."""
        db = _rows_db([_state(serial=1), _state(serial=2), _state(serial=3)])

        page = await replication.read_backfill(db, STATE_VERSIONS)

        assert [row["serial"] for row in page] == [1, 2, 3]

    @pytest.mark.replication_matrix("state_versions", "delta-apply")
    async def test_a_new_version_applies(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        sv = _state(serial=7)

        await replication.apply_upsert(
            db, STATE_VERSIONS, replication.serialize_row(STATE_VERSIONS, sv)
        )

        added = db.add.call_args[0][0]
        assert added.id == sv.id, "a replicated version must keep the peer's id"
        assert added.serial == 7

    @pytest.mark.replication_matrix("state_versions", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _state(serial=4)
        db.scalar.return_value = existing
        payload = replication.serialize_row(STATE_VERSIONS, existing)

        await replication.apply_upsert(db, STATE_VERSIONS, payload)
        await replication.apply_upsert(db, STATE_VERSIONS, payload)

        assert existing.serial == 4
        assert existing.md5 == "0" * 32

    @pytest.mark.replication_matrix("state_versions", "delete")
    async def test_deleting_a_version_applies(self):
        db = AsyncMock()
        sv = _state()

        await replication.apply_delete(db, STATE_VERSIONS, str(sv.id))

        assert db.execute.await_count == 1

    @pytest.mark.replication_matrix("state_versions", "backfill-converges-deletion")
    async def test_backfill_removes_versions_the_peer_no_longer_has(self):
        """State versions are prunable. A follower that only ever adds keeps
        serving a version the leader deliberately removed — and rollback would
        offer it."""
        keep, gone = str(uuid.uuid4()), str(uuid.uuid4())
        db = _keys_db([(keep,), (gone,)])

        removed = await replication.reconcile_deletions(db, STATE_VERSIONS, {keep})

        assert removed == [gone]


class TestTheRunLinkIsNotCarried:
    """`runs` is deliberately unreplicated — a run row is a live execution, not
    history. Carrying `run_id` would fail the insert against a run that is not
    there, trading a missing provenance link for a missing state version."""

    def test_run_id_is_excluded_from_the_wire(self):
        sv = _state(run_id=uuid.uuid4())

        payload = replication.serialize_row(STATE_VERSIONS, sv)

        assert "run_id" not in payload

    def test_the_version_still_carries_everything_that_identifies_it(self):
        sv = _state(run_id=uuid.uuid4())

        payload = replication.serialize_row(STATE_VERSIONS, sv)

        for field in ("id", "workspace_id", "serial", "lineage", "md5", "sha256"):
            assert field in payload, f"{field} is how the node finds and trusts the object"

    def test_runs_are_not_a_replicated_class(self):
        # Pinned so that registering `runs` becomes a deliberate act with the
        # reconciler consequences thought through, rather than a tidy-up.
        assert "runs" not in replication.registered()


class TestConfigurationVersions:
    @pytest.mark.replication_matrix("configuration_versions", "backfill-from-empty")
    async def test_backfill_carries_the_versions(self):
        db = _rows_db([_config(source="tfe-api"), _config(source="vcs")])

        page = await replication.read_backfill(db, CONFIG_VERSIONS)

        assert {row["source"] for row in page} == {"tfe-api", "vcs"}

    @pytest.mark.replication_matrix("configuration_versions", "delta-apply")
    async def test_a_new_version_applies(self):
        """A CLI-uploaded or catalog-provisioned workspace has no other copy of
        its configuration — losing this row means it can never run again, while
        the UI still lists it as healthy."""
        db = AsyncMock()
        db.add = MagicMock()
        db.scalar.return_value = None
        cv = _config(source="tfe-api")

        await replication.apply_upsert(
            db, CONFIG_VERSIONS, replication.serialize_row(CONFIG_VERSIONS, cv)
        )

        added = db.add.call_args[0][0]
        assert added.id == cv.id
        assert added.source == "tfe-api"

    @pytest.mark.replication_matrix("configuration_versions", "idempotent-reapply")
    async def test_reapplying_changes_nothing(self):
        db = AsyncMock()
        existing = _config(speculative=True)
        db.scalar.return_value = existing
        payload = replication.serialize_row(CONFIG_VERSIONS, existing)

        await replication.apply_upsert(db, CONFIG_VERSIONS, payload)
        await replication.apply_upsert(db, CONFIG_VERSIONS, payload)

        assert existing.speculative is True

    @pytest.mark.replication_matrix("configuration_versions", "delete")
    async def test_deleting_a_version_applies(self):
        db = AsyncMock()
        cv = _config()

        await replication.apply_delete(db, CONFIG_VERSIONS, str(cv.id))

        assert db.execute.await_count == 1

    @pytest.mark.replication_matrix("configuration_versions", "backfill-converges-deletion")
    async def test_backfill_removes_versions_the_peer_no_longer_has(self):
        keep, gone = str(uuid.uuid4()), str(uuid.uuid4())
        db = _keys_db([(keep,), (gone,)])

        removed = await replication.reconcile_deletions(db, CONFIG_VERSIONS, {keep})

        assert removed == [gone]


class TestOrdering:
    """Both classes are children of `workspaces`, so they must register after it
    or backfill inserts against a parent that is not there yet."""

    def test_both_come_after_workspaces(self):
        names = list(replication.registered())

        assert names.index("workspaces") < names.index("state_versions")
        assert names.index("workspaces") < names.index("configuration_versions")
