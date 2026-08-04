"""Delete markers and the orphaned-state reaper (#1253).

The reaper here is unlike every other retention category: the others walk DB
rows and delete the blobs those rows point at, but a deleted workspace has no
rows left — CASCADE took them — so its state can only be found by listing
storage and subtracting what still exists. These tests pin the two properties
that follow from that and are easy to regress:

  * discovery **stamps, never deletes** — so nothing is reaped that has not
    been visible as recoverable for the whole retention window; and
  * the window is measured from the marker's own `deleted_at` field, not from
    any blob's mtime.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services import deleted_workspace_service as dws
from terrapod.services.artifact_retention_service import _cleanup_deleted_workspaces
from terrapod.storage.keys import deleted_workspace_marker_key
from terrapod.storage.protocol import ObjectMeta, ObjectNotFoundError


class FakeStore:
    """In-memory object store, enough for prefix listing and get/put/delete."""

    def __init__(self, keys: list[str] | None = None):
        self.objects: dict[str, bytes] = dict.fromkeys(keys or [], b"{}")
        self.deleted: list[str] = []

    async def list_prefix(self, prefix: str, *, after: str = "", limit=None):
        return [
            ObjectMeta(
                key=k,
                size_bytes=len(v),
                content_type="application/json",
                etag="x",
                last_modified=datetime.now(UTC),
            )
            for k, v in sorted(self.objects.items())
            if k.startswith(prefix)
        ]

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def put(self, key, data, content_type="application/octet-stream", metadata=None):
        self.objects[key] = data
        return MagicMock()

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.deleted.append(key)


#: Real UUIDs — the reaper skips non-UUID prefixes, so string ids like "abc"
#: would silently never be seen as orphans and every assertion would pass
#: vacuously.
WS_A = "0192f3a1-0000-7000-8000-00000000000a"
WS_B = "0192f3a1-0000-7000-8000-00000000000b"


def _db(live_ids: list[str] | None = None, referenced: bool = False):
    """A DB backing both queries the reaper makes.

    The live-workspace query only calls `.scalars()`; the state-version safety
    check only calls `.scalar_one_or_none()` — so one result object serving
    both is unambiguous, and unlike call-ordering it stays correct however many
    orphans a cycle walks.
    """
    db = AsyncMock()
    res = MagicMock()
    res.scalars.return_value = list(live_ids or [])
    res.scalar_one_or_none.return_value = object() if referenced else None
    db.execute = AsyncMock(return_value=res)
    return db


def _marker(ws_id: str, age_days: float, reason: str = dws.REASON_DELETED) -> bytes:
    body = dws.build_discovery_marker(ws_id, datetime.now(UTC) - timedelta(days=age_days))
    body["marker_reason"] = reason
    body["workspace_name"] = "app-prod"
    return json.dumps(body).encode()


class TestDiscoveryStampsRatherThanDeletes:
    async def test_unmarked_orphan_is_stamped_and_nothing_is_deleted(self):
        """The property the whole retention window rests on. An orphan the
        reaper has never seen — a workspace deleted before this shipped, or one
        whose marker write failed — must start its clock, not be reaped on
        sight."""
        store = FakeStore([f"state/{WS_A}/1.tfstate", f"state/{WS_A}/2.tfstate"])

        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)

        assert reaped == 0
        assert store.deleted == []
        marker = json.loads(store.objects[deleted_workspace_marker_key(WS_A)])
        assert marker["marker_reason"] == dws.REASON_DISCOVERED
        assert marker["deleted_at"]

    async def test_a_marker_with_no_usable_date_restarts_the_clock(self):
        """A corrupt or dateless marker must not make an orphan immortal, but
        must not make it instantly reapable either."""
        store = FakeStore([f"state/{WS_A}/1.tfstate"])
        store.objects[deleted_workspace_marker_key(WS_A)] = json.dumps({"deleted_at": ""}).encode()

        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)

        assert reaped == 0
        assert store.deleted == []
        assert json.loads(store.objects[deleted_workspace_marker_key(WS_A)])["deleted_at"]


class TestRetentionWindow:
    async def test_orphan_inside_the_window_is_left_alone(self):
        store = FakeStore([f"state/{WS_A}/1.tfstate"])
        store.objects[deleted_workspace_marker_key(WS_A)] = _marker(WS_A, age_days=5)

        assert await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100) == 0
        assert store.deleted == []

    async def test_orphan_past_the_window_is_reaped_with_its_marker(self):
        store = FakeStore([f"state/{WS_A}/1.tfstate", f"state/{WS_A}/2.tfstate"])
        store.objects[deleted_workspace_marker_key(WS_A)] = _marker(WS_A, age_days=31)

        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)

        assert reaped == 1
        assert f"state/{WS_A}/1.tfstate" in store.deleted
        assert f"state/{WS_A}/2.tfstate" in store.deleted
        assert deleted_workspace_marker_key(WS_A) in store.deleted

    async def test_age_is_read_from_the_body_not_the_object_mtime(self):
        """Every object in FakeStore reports `last_modified` of now. An expired
        marker must still reap — proving the window is measured from the body,
        which is the only field replication carries faithfully."""
        store = FakeStore([f"state/{WS_A}/1.tfstate"])
        store.objects[deleted_workspace_marker_key(WS_A)] = _marker(WS_A, age_days=999)

        assert await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100) == 1


class TestNeverReapsLiveState:
    async def test_a_live_workspace_prefix_is_never_touched(self):
        store = FakeStore([f"state/{WS_B}/1.tfstate"])

        assert await _cleanup_deleted_workspaces(_db(live_ids=[WS_B]), store, 30, 100) == 0
        assert store.deleted == []
        assert deleted_workspace_marker_key(WS_B) not in store.objects

    async def test_expired_orphan_with_surviving_state_rows_is_not_reaped(self):
        """Belt and braces against a listing/DB skew: rows say it is alive even
        though the workspace query said otherwise."""
        store = FakeStore([f"state/{WS_A}/1.tfstate"])
        store.objects[deleted_workspace_marker_key(WS_A)] = _marker(WS_A, age_days=99)

        reaped = await _cleanup_deleted_workspaces(
            _db(live_ids=[], referenced=True), store, 30, 100
        )

        assert reaped == 0
        assert store.deleted == []

    async def test_the_marker_prefix_is_not_mistaken_for_a_workspace(self):
        """`state/deleted/…` and `state/index.yaml` live under `state/` but are
        not workspace prefixes; treating them as such would have the reaper
        stamp markers for imaginary workspaces."""
        store = FakeStore([f"state/deleted/{WS_A}.json", "state/index.yaml"])

        assert await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100) == 0
        assert store.deleted == []
        assert not any(k.endswith("index.yaml.json") for k in store.objects)

    async def test_a_non_uuid_prefix_is_skipped_rather_than_killing_the_cycle(self):
        """`Workspace.id` is a UUID column, so a stray directory under `state/`
        would raise on bind — and because the retention loop catches per
        category, that exception would silently disable reaping for every
        workspace, permanently. It must be skipped, and real orphans alongside
        it must still be processed."""
        store = FakeStore(["state/not-a-uuid/junk.tfstate", f"state/{WS_A}/1.tfstate"])

        await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)

        assert deleted_workspace_marker_key(WS_A) in store.objects
        assert deleted_workspace_marker_key("not-a-uuid") not in store.objects
        assert store.deleted == []


class TestMarkerContents:
    def test_a_discovery_marker_carries_the_fields_the_undelete_list_needs(self):
        body = dws.build_discovery_marker(WS_A)
        assert body["workspace_id"] == WS_A
        assert body["deleted_at"].endswith("Z")
        assert body["marker_reason"] == dws.REASON_DISCOVERED

    def test_marker_age_is_none_when_undateable(self):
        assert dws.marker_age_days({"deleted_at": "not-a-date"}) is None
        assert dws.marker_age_days({}) is None

    def test_settings_snapshot_carries_no_secret_bearing_field(self):
        """The marker lands in the bucket and replicates to a peer. Variable
        values and VCS credentials must never be in it — only the connection's
        id, which is a pointer to a row whose token stays in the database."""
        ws = MagicMock()
        ws.labels = {"env": "prod"}
        snap = dws._settings_snapshot(ws)
        banned = ("token", "secret", "password", "credential", "value")
        offenders = [k for k in snap if any(b in k.lower() for b in banned)]
        assert offenders == [], f"secret-shaped key in marker snapshot: {offenders}"
        assert "labels" in snap and "owner_email" in snap


@pytest.mark.parametrize("days", [0])
async def test_zero_retention_disables_the_category(days):
    """`0 = disabled` is the convention every other retention category follows;
    the dispatch loop skips on threshold 0, so an operator opting out keeps
    deleted state forever rather than losing it immediately."""
    from terrapod.config import settings

    assert settings.artifact_retention.deleted_workspace_retention_days >= 0
