"""Deleting registry content, against real Postgres and real storage (#1423).

Deletion is the half the collector was missing: it frees blobs nothing
references, and until now nothing could stop referencing them. Pushed content was
permanent by construction.

Like the collection tests, these are mostly about what must **survive** — a
delete that takes a layer another image is still using is the failure that
matters, and it is invisible until someone pulls the other image.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from terrapod.db.models import OCIBlob, OCIManifest, OCIRepository, OCIRepositoryBlob, OCITag
from terrapod.db.session import get_db_session
from terrapod.services.oci import gc, registry_service
from terrapod.storage import get_storage, keys

pytestmark = pytest.mark.asyncio


async def _repository(name: str) -> uuid.UUID:
    async with get_db_session() as db:
        repo = OCIRepository(name=name, labels={}, owner_email="a@b.c")
        db.add(repo)
        await db.flush()
        return repo.id


async def _blob(repo_id: uuid.UUID, content: bytes, *, age_hours: float = 48) -> str:
    """Store a blob, link it to a repository, and age the link past the grace window.

    Ageing is not incidental. The collector protects recently-arrived content so a
    push in flight is never swept, so a blob created during the test is correctly
    *not* reclaimed — and a test that skipped this would read as "deletion does
    not free anything" when the collector was doing its job.
    """
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    storage = get_storage()
    key = keys.oci_blob_key(digest.split(":", 1)[1])
    await storage.put(key, content)

    async with get_db_session() as db:
        existing = (
            await db.execute(select(OCIBlob).where(OCIBlob.digest == digest))
        ).scalar_one_or_none()
        if existing is None:
            existing = OCIBlob(digest=digest, size=len(content), storage_key=key)
            db.add(existing)
            await db.flush()
        already = (
            await db.execute(
                select(OCIRepositoryBlob).where(
                    OCIRepositoryBlob.repository_id == repo_id,
                    OCIRepositoryBlob.blob_id == existing.id,
                )
            )
        ).scalar_one_or_none()
        if already is None:
            link = OCIRepositoryBlob(repository_id=repo_id, blob_id=existing.id)
            db.add(link)
            await db.flush()
            await db.execute(
                update(OCIRepositoryBlob)
                .where(OCIRepositoryBlob.id == link.id)
                .values(created_at=datetime.now(UTC) - timedelta(hours=age_hours))
            )
    return digest


async def _manifest(repo_id: uuid.UUID, layer_digests: list[str], *, tag: str | None) -> str:
    """A manifest over the given layers, made distinct by its tag.

    Two images with identical layers are the same manifest — same bytes, same
    digest — so without something to tell them apart the second insert collides
    with the unique constraint rather than modelling two images.
    """
    document = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": "sha256:" + "0" * 64,
            "size": 2,
        },
        "layers": [
            {"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": d, "size": 1}
            for d in layer_digests
        ],
        "annotations": {"org.opencontainers.image.ref.name": tag or uuid.uuid4().hex},
    }
    body = json.dumps(document).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    storage = get_storage()
    key = keys.oci_manifest_key(digest.split(":", 1)[1])
    await storage.put(key, body)
    async with get_db_session() as db:
        manifest = OCIManifest(
            repository_id=repo_id,
            digest=digest,
            media_type=document["mediaType"],
            size=len(body),
            storage_key=key,
        )
        db.add(manifest)
        await db.flush()
        if tag is not None:
            db.add(OCITag(repository_id=repo_id, name=tag, manifest_id=manifest.id))
            await db.flush()
    return digest


async def _repo(repo_id: uuid.UUID) -> OCIRepository:
    async with get_db_session() as db:
        return (
            await db.execute(select(OCIRepository).where(OCIRepository.id == repo_id))
        ).scalar_one()


class TestDeletingATag:
    async def test_the_manifest_survives_and_becomes_untagged(self) -> None:
        """A tag is a name. Dropping the name must not destroy the thing named."""
        repo_id = await _repository(f"team/tagged-{uuid.uuid4().hex[:8]}")
        layer = await _blob(repo_id, b"layer-one")
        digest = await _manifest(repo_id, [layer], tag="v1")

        async with get_db_session() as db:
            repo = (
                await db.execute(select(OCIRepository).where(OCIRepository.id == repo_id))
            ).scalar_one()
            assert await registry_service.delete_tag(db, repo, "v1") is True
            await db.commit()

        async with get_db_session() as db:
            repo = (
                await db.execute(select(OCIRepository).where(OCIRepository.id == repo_id))
            ).scalar_one()
            still_there = await db.execute(select(OCIManifest).where(OCIManifest.digest == digest))
            assert still_there.scalar_one_or_none() is not None
            untagged = await registry_service.list_untagged_manifests(db, repo)
            assert [m.digest for m in untagged] == [digest]

    async def test_deleting_a_tag_that_is_not_there_reports_it(self) -> None:
        """So the endpoint can 404 rather than claim a success that deleted nothing."""
        repo_id = await _repository(f"team/none-{uuid.uuid4().hex[:8]}")
        async with get_db_session() as db:
            repo = (
                await db.execute(select(OCIRepository).where(OCIRepository.id == repo_id))
            ).scalar_one()
            assert await registry_service.delete_tag(db, repo, "absent") is False


class TestASharedLayer:
    """The failure that matters, and the reason deletion frees nothing directly.

    Two images share a base layer. Deleting one must leave the other pullable —
    and that cannot be decided by the delete, which only knows about the image in
    front of it.
    """

    async def test_it_survives_deleting_one_of_two_images(self) -> None:
        repo_id = await _repository(f"team/shared-{uuid.uuid4().hex[:8]}")
        base = await _blob(repo_id, b"shared-base-layer")
        only_a = await _blob(repo_id, b"only-in-a")
        digest_a = await _manifest(repo_id, [base, only_a], tag="a")
        digest_b = await _manifest(repo_id, [base], tag="b")

        async with get_db_session() as db:
            repo = (
                await db.execute(select(OCIRepository).where(OCIRepository.id == repo_id))
            ).scalar_one()
            assert await registry_service.delete_manifest(db, repo, digest_a) is True
            await db.commit()

        await gc.collect()

        async with get_db_session() as db:
            surviving = await db.execute(select(OCIBlob).where(OCIBlob.digest == base))
            assert surviving.scalar_one_or_none() is not None, (
                "the shared base layer was collected while image b still references it"
            )
            gone = await db.execute(select(OCIBlob).where(OCIBlob.digest == only_a))
            assert gone.scalar_one_or_none() is None, (
                "the layer unique to the deleted image should have been reclaimed"
            )
            b_still_there = await db.execute(
                select(OCIManifest).where(OCIManifest.digest == digest_b)
            )
            assert b_still_there.scalar_one_or_none() is not None

    async def test_it_is_freed_once_the_second_image_goes(self) -> None:
        repo_id = await _repository(f"team/both-{uuid.uuid4().hex[:8]}")
        base = await _blob(repo_id, b"base-for-both-images")
        digest_a = await _manifest(repo_id, [base], tag="a")
        digest_b = await _manifest(repo_id, [base], tag="b")

        async with get_db_session() as db:
            repo = (
                await db.execute(select(OCIRepository).where(OCIRepository.id == repo_id))
            ).scalar_one()
            await registry_service.delete_manifest(db, repo, digest_a)
            await registry_service.delete_manifest(db, repo, digest_b)
            await db.commit()

        await gc.collect()

        async with get_db_session() as db:
            gone = await db.execute(select(OCIBlob).where(OCIBlob.digest == base))
            assert gone.scalar_one_or_none() is None


class TestDeletingByDigest:
    async def test_its_tags_go_with_it(self) -> None:
        """A tag naming a manifest that no longer exists is worse than no tag."""
        repo_id = await _repository(f"team/bydigest-{uuid.uuid4().hex[:8]}")
        layer = await _blob(repo_id, b"digest-delete-layer")
        digest = await _manifest(repo_id, [layer], tag="v1")

        async with get_db_session() as db:
            repo = (
                await db.execute(select(OCIRepository).where(OCIRepository.id == repo_id))
            ).scalar_one()
            await registry_service.delete_manifest(db, repo, digest)
            await db.commit()

        async with get_db_session() as db:
            tags = await db.execute(
                select(func.count()).select_from(OCITag).where(OCITag.repository_id == repo_id)
            )
            assert tags.scalar_one() == 0


class TestDeletingARepository:
    async def test_it_takes_its_own_content_and_leaves_a_shared_blob(self) -> None:
        """A blob row is shared across repositories; only the link is this one's."""
        keep_id = await _repository(f"team/keep-{uuid.uuid4().hex[:8]}")
        drop_id = await _repository(f"team/drop-{uuid.uuid4().hex[:8]}")
        shared = await _blob(keep_id, b"shared-between-repositories")
        await _blob(drop_id, b"shared-between-repositories")
        await _manifest(keep_id, [shared], tag="keep")
        await _manifest(drop_id, [shared], tag="drop")

        async with get_db_session() as db:
            repo = (
                await db.execute(select(OCIRepository).where(OCIRepository.id == drop_id))
            ).scalar_one()
            counts = await registry_service.delete_repository(db, repo)
            await db.commit()
        assert counts["tags"] == 1 and counts["manifests"] == 1

        await gc.collect()

        async with get_db_session() as db:
            assert (
                await db.execute(select(OCIRepository).where(OCIRepository.id == drop_id))
            ).scalar_one_or_none() is None
            assert (
                await db.execute(select(OCIBlob).where(OCIBlob.digest == shared))
            ).scalar_one_or_none() is not None, (
                "a blob the surviving repository still references was collected"
            )
