"""Concurrent duplicate pushes do not collide (#2562).

Four writes on the registry's push path each promised idempotency in their
docstrings and implemented it as a SELECT above an INSERT. That is idempotent
one request at a time and not two: `docker push` sends blobs concurrently, and
a client retrying an interrupted push re-sends everything, so two requests for
one digest both saw no row and both inserted. The loser raised a unique
violation, which the app-wide net turned into a JSON:API 409 -- a shape a
container client cannot read, so `docker push` reported `unknown error`.

**These do NOT prove the race, and saying so is the point.** They were written
to, through separate sessions and `asyncio.gather`, and then measured against
the check-then-act code: all five PASSED. The two coroutines do not reliably
interleave between the SELECT and the INSERT, so the collision never happens
and the test reads as proof of something it never exercised.

What they do cover is real and worth keeping: each write is driven twice
through SEPARATE sessions against real Postgres and must leave exactly one row,
which is the idempotency the docstrings promise, and the last one pins the
`revalidated_at` rule that the upsert had to carry across. The conflict action
itself -- that the database rather than a read-guard is what tolerates the
duplicate -- is pinned by
`tests/services/test_oci_writes_are_conflict_tolerant.py`, which fails on the
pre-fix code where these do not.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from sqlalchemy import func, select

from terrapod.db.models import OCIBlob, OCIManifest, OCIRepository, OCIRepositoryBlob, OCITag
from terrapod.db.session import get_db_session
from terrapod.services.oci import registry_service, upload_service

pytestmark = pytest.mark.asyncio


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


async def _repository(name: str) -> uuid.UUID:
    async with get_db_session() as db:
        repo = OCIRepository(name=name)
        db.add(repo)
        await db.flush()
        rid = repo.id
        await db.commit()
    return rid


async def _load(session, model, rid):
    return (await session.execute(select(model).where(model.id == rid))).scalar_one()


class TestConcurrentBlobRegistration:
    async def test_two_sessions_registering_one_digest_both_succeed(self):
        digest = _digest(f"blob-{uuid.uuid4()}")

        async def register():
            async with get_db_session() as db:
                blob = await upload_service._upsert_blob(db, digest, 11, f"blobs/{digest}")
                await db.commit()
                return blob.digest

        # Both must return the digest. Under the check-then-act form the loser
        # raised `UniqueViolationError` on `oci_blobs.digest` instead.
        results = await asyncio.gather(register(), register())
        assert results == [digest, digest], results

        async with get_db_session() as db:
            n = (
                await db.execute(
                    select(func.count()).select_from(OCIBlob).where(OCIBlob.digest == digest)
                )
            ).scalar_one()
        assert n == 1, f"expected exactly one blob row, found {n}"


class TestConcurrentBlobLinking:
    async def test_two_sessions_linking_one_blob_to_one_repository_both_succeed(self):
        rid = await _repository(f"concur/link-{uuid.uuid4().hex[:8]}")
        digest = _digest(f"link-{uuid.uuid4()}")
        async with get_db_session() as db:
            blob = await upload_service._upsert_blob(db, digest, 7, f"blobs/{digest}")
            bid = blob.id
            await db.commit()

        async def link():
            async with get_db_session() as db:
                repo = await _load(db, OCIRepository, rid)
                blob = await _load(db, OCIBlob, bid)
                await upload_service.link_blob(db, repo, blob)
                await db.commit()

        await asyncio.gather(link(), link())

        async with get_db_session() as db:
            n = (
                await db.execute(
                    select(func.count())
                    .select_from(OCIRepositoryBlob)
                    .where(
                        OCIRepositoryBlob.repository_id == rid,
                        OCIRepositoryBlob.blob_id == bid,
                    )
                )
            ).scalar_one()
        assert n == 1, f"expected exactly one link row, found {n}"


class TestConcurrentManifestStore:
    async def test_two_sessions_storing_one_manifest_both_succeed(self, tmp_path):
        from terrapod.storage.filesystem import FilesystemStore

        rid = await _repository(f"concur/manifest-{uuid.uuid4().hex[:8]}")
        body = b'{"schemaVersion":2,"layers":[]}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        store = FilesystemStore(str(tmp_path))

        async def push():
            async with get_db_session() as db:
                repo = await _load(db, OCIRepository, rid)
                m = await registry_service.store_manifest(
                    db, store, repo, digest, "application/vnd.oci.image.manifest.v1+json", body
                )
                await db.commit()
                return m.digest

        results = await asyncio.gather(push(), push())
        assert results == [digest, digest], results

        async with get_db_session() as db:
            n = (
                await db.execute(
                    select(func.count())
                    .select_from(OCIManifest)
                    .where(OCIManifest.repository_id == rid, OCIManifest.digest == digest)
                )
            ).scalar_one()
        assert n == 1, f"expected exactly one manifest row, found {n}"


class TestConcurrentTagSet:
    """A tag MOVES, so this one upserts with DO UPDATE rather than DO NOTHING."""

    async def test_two_sessions_setting_one_tag_both_succeed(self, tmp_path):
        from terrapod.storage.filesystem import FilesystemStore

        rid = await _repository(f"concur/tag-{uuid.uuid4().hex[:8]}")
        store = FilesystemStore(str(tmp_path))
        manifests = []
        for i in (1, 2):
            body = b'{"schemaVersion":2,"n":%d}' % i
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            async with get_db_session() as db:
                repo = await _load(db, OCIRepository, rid)
                m = await registry_service.store_manifest(
                    db, store, repo, digest, "application/vnd.oci.image.manifest.v1+json", body
                )
                manifests.append(m.id)
                await db.commit()

        async def tag(mid):
            async with get_db_session() as db:
                repo = await _load(db, OCIRepository, rid)
                m = await _load(db, OCIManifest, mid)
                t = await registry_service.set_tag(db, repo, "latest", m)
                await db.commit()
                return t.manifest_id

        # Both complete; the tag ends on one of the two, not in a 500.
        results = await asyncio.gather(tag(manifests[0]), tag(manifests[1]))
        assert all(r in manifests for r in results), results

        async with get_db_session() as db:
            rows = (
                (
                    await db.execute(
                        select(OCITag).where(OCITag.repository_id == rid, OCITag.name == "latest")
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, f"expected exactly one tag row, found {len(rows)}"
        assert rows[0].manifest_id in manifests

    async def test_a_local_push_still_leaves_revalidated_at_null(self, tmp_path):
        """The rule `set_tag`'s docstring rests on, preserved through the upsert.

        `revalidated_at` is what separates a mirrored tag from a pushed one, and
        only a mirrored tag is revalidated against upstream. The DO UPDATE set
        therefore touches that column only when `from_upstream`, or a local push
        would claim a confirmation it never made.
        """
        from terrapod.storage.filesystem import FilesystemStore

        rid = await _repository(f"concur/stamp-{uuid.uuid4().hex[:8]}")
        store = FilesystemStore(str(tmp_path))
        body = b'{"schemaVersion":2,"stamp":true}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()

        async with get_db_session() as db:
            repo = await _load(db, OCIRepository, rid)
            m = await registry_service.store_manifest(
                db, store, repo, digest, "application/vnd.oci.image.manifest.v1+json", body
            )
            # Mirrored first, so the column is non-NULL and a later local push
            # has something to wrongly preserve or wrongly clear.
            t = await registry_service.set_tag(db, repo, "v1", m, from_upstream=True)
            assert t.revalidated_at is not None, "an upstream fetch did not stamp it"
            stamped = t.revalidated_at
            await db.commit()

        async with get_db_session() as db:
            repo = await _load(db, OCIRepository, rid)
            m = await _load(db, OCIManifest, m.id)
            t = await registry_service.set_tag(db, repo, "v1", m, from_upstream=False)
            await db.commit()
            assert t.revalidated_at == stamped, (
                "a local push moved `revalidated_at`, so it claims an upstream "
                "confirmation that never happened"
            )
