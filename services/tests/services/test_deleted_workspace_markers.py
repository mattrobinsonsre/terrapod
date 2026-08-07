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

import contextlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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

        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)
        assert reaped == 0
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

        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)
        assert reaped == 1


class TestNeverReapsLiveState:
    async def test_a_live_workspace_prefix_is_never_touched(self):
        store = FakeStore([f"state/{WS_B}/1.tfstate"])

        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[WS_B]), store, 30, 100)
        assert reaped == 0
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

        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)
        assert reaped == 0
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


@contextlib.asynccontextmanager
async def _fake_db_session():
    """The cycle opens a session per category; none of these tests touch the
    database, so a stub keeps the run about dispatch rather than about
    connectivity."""
    yield AsyncMock()


async def test_zero_retention_never_reaches_the_reaper():
    """`0 = disabled` is the convention every other retention category
    follows, and the direction matters more than the convention: an operator
    opting out must keep deleted state **forever**, not lose it immediately.

    It is enforced by the dispatch loop, not by the handler — pass 0 straight
    to `_cleanup_deleted_workspaces` and it happily reaps everything, because
    every marker is older than a zero-day window. So this drives the cycle and
    asserts the handler is never reached at all.

    (Replaces a test that asserted the configured value was `>= 0`, which is
    true of every possible configuration including the broken one — #1297.)
    """
    from terrapod.config import settings
    from terrapod.services import artifact_retention_service as ars

    original = settings.artifact_retention.deleted_workspace_retention_days
    settings.artifact_retention.deleted_workspace_retention_days = 0
    called = False

    async def _spy(*a, **kw):
        nonlocal called
        called = True
        return 0

    try:
        with (
            patch.object(ars, "_cleanup_deleted_workspaces", _spy),
            patch("terrapod.storage.get_storage", return_value=FakeStore()),
            patch("terrapod.db.session.get_db_session", _fake_db_session),
        ):
            await ars.artifact_retention_cycle()
    finally:
        settings.artifact_retention.deleted_workspace_retention_days = original

    assert not called, "a zero window must disable the category, not expire everything in it"


async def test_a_positive_retention_does_reach_the_reaper():
    """Pins the contrast, so the test above cannot pass merely because the
    category has been unregistered or the cycle is broken outright."""
    from terrapod.config import settings
    from terrapod.services import artifact_retention_service as ars

    original = settings.artifact_retention.deleted_workspace_retention_days
    settings.artifact_retention.deleted_workspace_retention_days = 30
    called = False

    async def _spy(*a, **kw):
        nonlocal called
        called = True
        return 0

    try:
        with (
            patch.object(ars, "_cleanup_deleted_workspaces", _spy),
            patch("terrapod.storage.get_storage", return_value=FakeStore()),
            patch("terrapod.db.session.get_db_session", _fake_db_session),
        ):
            await ars.artifact_retention_cycle()
    finally:
        settings.artifact_retention.deleted_workspace_retention_days = original

    assert called


class TestMarkerWriteNeverFailsTheDelete:
    """`write_marker_best_effort` exists for exactly one reason: the delete has
    already committed by the time it runs. Raising would return a 500 for a
    workspace that IS gone — the operator retries, gets a 404, and is left
    believing the delete failed (#1297). Only the happy path was exercised.
    """

    async def test_a_storage_failure_is_swallowed(self):
        storage = MagicMock()
        storage.put = AsyncMock(side_effect=RuntimeError("bucket unreachable"))
        with patch.object(dws, "get_storage", return_value=storage):
            await dws.write_marker_best_effort(WS_A, {"workspace_id": WS_A})

    async def test_an_unresolvable_store_is_swallowed_too(self):
        """Resolving the store is inside the guard, not before it: an
        unconfigured or unavailable backend must degrade to "no marker" — the
        reaper stamps one on a later cycle — never to a failed delete."""
        with patch.object(dws, "get_storage", side_effect=RuntimeError("no storage configured")):
            await dws.write_marker_best_effort(WS_A, {"workspace_id": WS_A})

    async def test_an_unserialisable_body_is_swallowed(self):
        """The failure need not come from the network. A body carrying
        something json cannot encode still must not sink the delete."""
        storage = MagicMock()
        storage.put = AsyncMock()
        with patch.object(dws, "get_storage", return_value=storage):
            await dws.write_marker_best_effort(WS_A, {"bad": object()})

    async def test_the_happy_path_still_writes_the_marker(self):
        """Swallowing everything is only correct if the normal case works —
        otherwise the guard would hide a permanently broken writer."""
        store = FakeStore()
        with patch.object(dws, "get_storage", return_value=store):
            await dws.write_marker_best_effort(WS_A, {"workspace_id": WS_A})
        assert deleted_workspace_marker_key(WS_A) in store.objects


class TestReapIsObservable:
    """Every other retention category increments `RETENTION_DELETED`; this one
    did not (#1299) — leaving the only category that irreversibly destroys
    customer state as the only one an operator could not graph, alert on, or
    reconcile against afterwards. A deletion nobody can see happen is
    indistinguishable from one that never happened."""

    async def test_a_reap_increments_the_retention_counter(self):
        from terrapod.api.metrics import RETENTION_DELETED

        counter = RETENTION_DELETED.labels(category="deleted_workspaces")
        before = counter._value.get()

        store = FakeStore([f"state/{WS_A}/1.tfstate"])
        store.objects[deleted_workspace_marker_key(WS_A)] = _marker(WS_A, age_days=31)
        reaped = await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100)

        assert reaped == 1
        assert counter._value.get() == before + 1

    async def test_a_cycle_that_reaps_nothing_does_not_move_the_counter(self):
        """A counter that ticks on every cycle regardless is worse than none —
        it makes "did we destroy anything last night" unanswerable."""
        from terrapod.api.metrics import RETENTION_DELETED

        counter = RETENTION_DELETED.labels(category="deleted_workspaces")
        before = counter._value.get()

        store = FakeStore([f"state/{WS_B}/1.tfstate"])
        store.objects[deleted_workspace_marker_key(WS_B)] = _marker(WS_B, age_days=5)
        assert await _cleanup_deleted_workspaces(_db(live_ids=[]), store, 30, 100) == 0

        assert counter._value.get() == before


class TestMassOrphanBreaker:
    """This category inverts the safety property of every other one: it deletes
    what the DB does not claim, so what it destroys depends on the database
    being COMPLETE, not on it being correct. A database restored from an older
    backup, or a `DATABASE_URL` repointed mid-migration, makes every missing
    workspace read as an orphan — and the existing `still_referenced` re-check
    cannot help, because missing rows are exactly what a stale DB has (#1299).
    """

    @staticmethod
    def _expired_orphans(n: int) -> FakeStore:
        store = FakeStore()
        for i in range(n):
            ws = f"0192f3a1-0000-7000-8000-{i:012d}"
            store.objects[f"state/{ws}/1.tfstate"] = b"{}"
            store.objects[deleted_workspace_marker_key(ws)] = _marker(ws, age_days=31)
        return store

    @staticmethod
    def _db_with_live_count(live_total: int):
        """Distinguishes the live-workspace COUNT from the per-orphan
        state-version safety check, which both read `scalar_one_or_none` but
        must answer differently — a shared value would make every orphan look
        still-referenced and the assertions pass vacuously."""

        async def execute(stmt, *a, **kw):
            res = MagicMock()
            res.scalars.return_value = []
            res.scalar_one_or_none.return_value = (
                live_total if "count(" in str(stmt).lower() else None
            )
            return res

        db = AsyncMock()
        db.execute = execute
        return db

    async def test_an_implausible_orphan_set_reaps_nothing(self):
        # 40 expired orphans against 10 live workspaces: far past the 0.5 ratio.
        store = self._expired_orphans(40)
        reaped = await _cleanup_deleted_workspaces(self._db_with_live_count(10), store, 30, 100)

        assert reaped == 0
        # Not one object touched — the refusal is total, not partial.
        assert store.deleted == []

    async def test_the_refusal_is_visible_to_an_operator(self):
        from terrapod.api.metrics import RETENTION_ORPHAN_REAP_BLOCKED

        store = self._expired_orphans(40)
        await _cleanup_deleted_workspaces(self._db_with_live_count(10), store, 30, 100)
        assert RETENTION_ORPHAN_REAP_BLOCKED._value.get() == 1

        # ...and clears itself once the ratio is plausible again, so the alert
        # resolves without an operator having to reset anything.
        small = self._expired_orphans(3)
        await _cleanup_deleted_workspaces(self._db_with_live_count(100), small, 30, 100)
        assert RETENTION_ORPHAN_REAP_BLOCKED._value.get() == 0

    async def test_a_small_orphan_set_is_never_blocked(self):
        """A pure ratio would refuse to reap on a brand-new deployment with
        nothing live to protect, and on a small one that legitimately has more
        deleted workspaces than live ones."""
        store = self._expired_orphans(3)
        reaped = await _cleanup_deleted_workspaces(self._db_with_live_count(0), store, 30, 100)
        assert reaped == 3

    async def test_a_large_but_proportionate_orphan_set_still_reaps(self):
        """The breaker must not become a permanent stop on a big deployment
        that deletes workspaces at a normal rate."""
        store = self._expired_orphans(30)
        reaped = await _cleanup_deleted_workspaces(self._db_with_live_count(500), store, 30, 100)
        assert reaped == 30
