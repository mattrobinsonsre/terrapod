"""The registry's push writes tolerate a duplicate AT THE DATABASE (#2562).

Four writes on the push path promised idempotency in their docstrings and
implemented it as a SELECT above an INSERT. That is idempotent one request at a
time and not two: `docker push` sends blobs concurrently and a retried push
re-sends everything, so two requests for one key could both see no row and both
insert. The loser raised a unique violation, the app-wide net turned it into a
JSON:API 409, and `docker push` reported `unknown error` because that envelope
is not one a container client can read.

Asserted on the COMPILED STATEMENT rather than on behaviour, deliberately. The
first version of this guard was a real-Postgres test driving two sessions
through `asyncio.gather`, and it **passed against the check-then-act code** --
the two coroutines did not interleave between the SELECT and the INSERT, so the
race it claimed to provoke never happened. A concurrency test that cannot be
made to fail is worth less than nothing, because it reads as proof. What the
database guarantees is `ON CONFLICT`, so that is what is pinned here, and the
integration tests beside it are honest about covering the path rather than the
race.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from terrapod.services.oci import registry_service, upload_service

pytestmark = pytest.mark.asyncio


def _sql(statement) -> str:
    """The statement as PostgreSQL will see it."""
    return str(statement.compile(dialect=postgresql.dialect()))


class _Recorder:
    """A session that records every statement and answers reads plausibly."""

    def __init__(self, row=None):
        self.statements: list = []
        self._row = row if row is not None else MagicMock()
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        result = MagicMock()
        result.scalar_one.return_value = self._row
        result.scalar_one_or_none.return_value = self._row
        return result

    @property
    def sql(self) -> list[str]:
        return [_sql(s) for s in self.statements]


def _upserts(sqls: list[str]) -> list[str]:
    return [s for s in sqls if "ON CONFLICT" in s.upper()]


class TestEveryPushWriteUsesOnConflict:
    async def test_blob_registration(self):
        db = _Recorder()
        await upload_service._upsert_blob(db, "sha256:" + "a" * 64, 9, "blobs/x")
        found = _upserts(db.sql)
        assert found, f"no ON CONFLICT in: {db.sql}"
        assert "DO NOTHING" in found[0].upper(), found[0]
        assert "oci_blobs" in found[0], found[0]

    async def test_blob_to_repository_link(self):
        db = _Recorder()
        repo = MagicMock(id=uuid.uuid4())
        blob = MagicMock(id=uuid.uuid4())
        await upload_service.link_blob(db, repo, blob)
        found = _upserts(db.sql)
        assert found, f"no ON CONFLICT in: {db.sql}"
        assert "DO NOTHING" in found[0].upper(), found[0]
        assert "oci_repository_blobs" in found[0], found[0]

    async def test_manifest_store(self):
        db = _Recorder()
        storage = MagicMock()
        storage.put = AsyncMock()
        repo = MagicMock(id=uuid.uuid4())
        body = b'{"schemaVersion":2}'
        await registry_service.store_manifest(
            db, storage, repo, "sha256:" + "b" * 64, "application/json", body
        )
        found = _upserts(db.sql)
        assert found, f"no ON CONFLICT in: {db.sql}"
        assert "DO NOTHING" in found[0].upper(), found[0]
        assert "oci_manifests" in found[0], found[0]

    async def test_tag_set_updates_rather_than_does_nothing(self):
        """A tag MOVES, so DO NOTHING here would silently refuse a re-tag.

        This is the one write whose conflict action must not be DO NOTHING:
        pointing `latest` at a new manifest is the whole purpose of a mutable
        reference, and ignoring the conflict would leave it on the old one while
        answering 201.
        """
        db = _Recorder()
        repo = MagicMock(id=uuid.uuid4())
        manifest = MagicMock(id=uuid.uuid4())
        await registry_service.set_tag(db, repo, "latest", manifest)
        found = _upserts(db.sql)
        assert found, f"no ON CONFLICT in: {db.sql}"
        upsert = found[0].upper()
        assert "DO UPDATE" in upsert, f"a re-tag would be ignored: {found[0]}"
        assert "DO NOTHING" not in upsert, found[0]
        assert "oci_tags" in found[0], found[0]


class TestNoWriteReadsBeforeItWrites:
    """The read-guard is gone, not merely joined by an ON CONFLICT.

    A `SELECT` left above the insert would still race -- it would simply race
    and then be rescued, which is fine, but it also reads as the guard and
    invites someone to 'simplify' the ON CONFLICT away again. These pin that the
    write is the FIRST statement each function issues.
    """

    async def test_blob_registration_writes_first(self):
        db = _Recorder()
        await upload_service._upsert_blob(db, "sha256:" + "c" * 64, 9, "blobs/x")
        assert "ON CONFLICT" in db.sql[0].upper(), (
            f"the first statement is a read, so the guard is back: {db.sql[0]}"
        )

    async def test_manifest_store_writes_first(self):
        db = _Recorder()
        storage = MagicMock()
        storage.put = AsyncMock()
        repo = MagicMock(id=uuid.uuid4())
        await registry_service.store_manifest(
            db, storage, repo, "sha256:" + "d" * 64, "application/json", b"{}"
        )
        assert "ON CONFLICT" in db.sql[0].upper(), (
            f"the first statement is a read, so the guard is back: {db.sql[0]}"
        )

    async def test_tag_set_writes_first(self):
        db = _Recorder()
        repo = MagicMock(id=uuid.uuid4())
        manifest = MagicMock(id=uuid.uuid4())
        await registry_service.set_tag(db, repo, "v1", manifest)
        assert "ON CONFLICT" in db.sql[0].upper(), (
            f"the first statement is a read, so the guard is back: {db.sql[0]}"
        )
