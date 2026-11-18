"""The orphaned-object collector (#2599).

Integration rather than services-tier because every property under test is a
Postgres one: the recorder is an `ON CONFLICT DO NOTHING` upsert against a
unique index, and the sweep's safety guard is a query across three tables. A
mocked session would assert that functions were called and prove none of it.

What this exists to prevent: replacing a pulled artifact with an operator upload
moves the row's key from `cache/` to `uploads/`, and every retention sweep in
the tree is row-driven -- it walks rows and deletes the object each one names.
An object with no row is invisible to all of them, so without this it is kept
for ever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from terrapod.db.models import CachedBinary, OrphanedObject
from terrapod.db.session import get_db_session
from terrapod.services import orphaned_objects


class _FakeStorage:
    """Records deletes and can be told to fail one.

    Not a MagicMock: the sweep's handling of a failed delete is a property under
    test, and `side_effect` juggling reads worse than six lines.
    """

    def __init__(self, *, failing: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self.failing = failing or set()

    async def delete(self, key: str) -> None:
        if key in self.failing:
            raise RuntimeError("storage is briefly unavailable")
        self.deleted.append(key)


async def _record(key: str, *, reason: str = "replaced by an operator upload") -> None:
    async with get_db_session() as db:
        await orphaned_objects.record(db, key, reason=reason)
        await db.commit()


async def _rows() -> list[str]:
    async with get_db_session() as db:
        return list((await db.execute(sa.select(OrphanedObject.storage_key))).scalars().all())


async def _sweep(storage: _FakeStorage, **kwargs) -> tuple[int, int]:
    async with get_db_session() as db:
        got = await orphaned_objects.sweep(db, storage, **kwargs)
        await db.commit()
    return got


@pytest.mark.asyncio
class TestRecording:
    async def test_a_key_is_recorded(self, app) -> None:
        await _record("cache/binaries/tofu/1.9.0/linux_amd64")
        assert await _rows() == ["cache/binaries/tofu/1.9.0/linux_amd64"]

    async def test_recording_the_same_key_twice_does_not_raise(self, app) -> None:
        """Re-uploading over the same artifact orphans the same object again.

        The second upload must not fail because of bookkeeping, so the insert is
        an upsert. Without `ON CONFLICT DO NOTHING` this raises
        `UniqueViolationError` and takes the operator's upload down with it.
        """
        key = "cache/binaries/tofu/1.9.0/linux_amd64"
        await _record(key)
        await _record(key, reason="a second replacement")
        assert await _rows() == [key]

    async def test_the_first_reason_is_kept(self, app) -> None:
        """It is when the object actually became unreferenced."""
        key = "cache/binaries/tofu/1.9.0/linux_amd64"
        await _record(key, reason="first")
        await _record(key, reason="second")
        async with get_db_session() as db:
            got = await db.scalar(sa.select(OrphanedObject.reason))
        assert got == "first"

    async def test_an_empty_key_records_nothing(self, app) -> None:
        """A row whose key was NULL has no object to orphan."""
        await _record("")
        assert await _rows() == []

    async def test_recording_does_not_commit(self, app) -> None:
        """The caller's transaction decides -- the whole point of recording.

        A rollback after a successful put must take the record with it, because
        the row reverts to the old key and the object is referenced again.
        """
        async with get_db_session() as db:
            await orphaned_objects.record(db, "cache/x", reason="r")
            await db.rollback()
        assert await _rows() == []


@pytest.mark.asyncio
class TestSweeping:
    async def test_an_unreferenced_object_is_deleted(self, app) -> None:
        key = "cache/binaries/tofu/1.9.0/linux_amd64"
        await _record(key)
        storage = _FakeStorage()
        assert await _sweep(storage) == (1, 0)
        assert storage.deleted == [key]
        assert await _rows() == []

    async def test_a_key_that_came_back_into_use_is_kept(self, app) -> None:
        """The guard that makes a stale record harmless.

        An operator uploads over an artifact, then re-uploads the original
        bytes: the pull-through key is live again, and deleting it would destroy
        an object that is being served. The record goes; the object stays.
        """
        key = "cache/binaries/tofu/1.9.0/linux_amd64"
        await _record(key)
        async with get_db_session() as db:
            db.add(
                CachedBinary(
                    id=uuid.uuid4(),
                    tool="tofu",
                    version="1.9.0",
                    os="linux",
                    arch="amd64",
                    shasum="abc",
                    download_url="",
                    storage_key=key,
                    cached_at=datetime.now(UTC),
                    last_accessed_at=datetime.now(UTC),
                )
            )
            await db.commit()

        storage = _FakeStorage()
        assert await _sweep(storage) == (0, 1)
        assert storage.deleted == [], "a live object was deleted"
        assert await _rows() == []

    async def test_a_failed_delete_leaves_the_record_for_the_next_pass(self, app) -> None:
        """Forgetting it would turn a transient storage blip into a permanent
        leak, which is the thing this table exists to stop.
        """
        key = "cache/binaries/tofu/1.9.0/linux_amd64"
        await _record(key)
        assert await _sweep(_FakeStorage(failing={key})) == (0, 0)
        assert await _rows() == [key]

        # And the retry succeeds.
        storage = _FakeStorage()
        assert await _sweep(storage) == (1, 0)
        assert storage.deleted == [key]

    async def test_the_pass_is_bounded(self, app) -> None:
        """One enormous backlog must not hold a transaction open for minutes."""
        for i in range(5):
            await _record(f"cache/binaries/tofu/1.{i}.0/linux_amd64")
        storage = _FakeStorage()
        assert await _sweep(storage, limit=2) == (2, 0)
        assert len(await _rows()) == 3

    async def test_an_empty_table_sweeps_nothing(self, app) -> None:
        storage = _FakeStorage()
        assert await _sweep(storage) == (0, 0)
        assert storage.deleted == []
