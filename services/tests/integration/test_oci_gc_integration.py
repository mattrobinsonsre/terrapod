"""Garbage collection against real Postgres and real storage (#1419).

This is the feature where being wrong deletes someone's data, so the tests are
mostly about what must *survive* a collection rather than what it reclaims.

Three properties carry the design, and each has a test that fails without it:

* a shared layer survives while anything still references it;
* an image an operator **pushed** is never expired by age, because it exists
  nowhere else;
* a push in flight is never collected — blobs land before the manifest that
  references them, so a sweep in that window would delete the layers of a push
  that is still running.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from terrapod.db.models import (
    OCIBlob,
    OCIManifest,
    OCIRepository,
    OCIRepositoryBlob,
    OCITag,
)
from terrapod.db.session import get_db_session
from terrapod.services.oci import gc
from terrapod.storage import get_storage, keys

pytestmark = pytest.mark.asyncio


async def _repository(name: str, upstream: str | None = None) -> uuid.UUID:
    async with get_db_session() as db:
        repo = OCIRepository(name=name, labels={}, owner_email="a@b.c", upstream=upstream)
        db.add(repo)
        await db.flush()
        return repo.id


async def _blob(repo_id: uuid.UUID, content: bytes, *, age_hours: float = 48) -> str:
    """Store a blob, link it to a repository, and age the link."""
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
        link = OCIRepositoryBlob(repository_id=repo_id, blob_id=existing.id)
        db.add(link)
        await db.flush()
        await db.execute(
            update(OCIRepositoryBlob)
            .where(OCIRepositoryBlob.id == link.id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=age_hours))
        )
    return digest


async def _manifest(repo_id: uuid.UUID, document: dict, *, tag: str | None = None) -> str:
    body = json.dumps(document).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    storage = get_storage()
    key = keys.oci_manifest_key(digest.split(":", 1)[1])
    await storage.put(key, body)

    async with get_db_session() as db:
        manifest = OCIManifest(
            repository_id=repo_id,
            digest=digest,
            media_type="application/vnd.oci.image.manifest.v1+json",
            size=len(body),
            storage_key=key,
            subject_digest=(document.get("subject") or {}).get("digest"),
        )
        db.add(manifest)
        await db.flush()
        if tag:
            db.add(OCITag(repository_id=repo_id, name=tag, manifest_id=manifest.id))
            await db.flush()
    return digest


def _image(*layer_digests: str) -> dict:
    return {
        "schemaVersion": 2,
        "config": {
            "digest": layer_digests[0],
            "mediaType": "application/vnd.oci.image.config.v1+json",
        },
        "layers": [
            {"digest": d, "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
            for d in layer_digests[1:]
        ],
    }


async def _age_all_manifests(*, days: int) -> None:
    """Make every manifest old, by both clocks, so neither could excuse a delete."""
    async with get_db_session() as db:
        await db.execute(
            update(OCIManifest).values(
                created_at=datetime.now(UTC) - timedelta(days=days),
                last_accessed_at=datetime.now(UTC) - timedelta(days=days),
            )
        )


async def _tagged_digest(repo_id: uuid.UUID) -> str:
    async with get_db_session() as db:
        row = await db.execute(
            select(OCIManifest.digest)
            .join(OCITag, OCITag.manifest_id == OCIManifest.id)
            .where(OCITag.repository_id == repo_id)
        )
        return row.scalar_one()


async def _blob_count() -> int:
    async with get_db_session() as db:
        return await db.scalar(select(func.count()).select_from(OCIBlob)) or 0


class TestSharedLayers:
    async def test_a_layer_two_images_share_is_not_collected(self, app) -> None:
        """The reason a delete endpoint would not have worked.

        Deleting one image must not take out a base layer the other still needs.
        """
        repo = await _repository("team/app")
        config_a = await _blob(repo, b"config-a")
        config_b = await _blob(repo, b"config-b")
        shared = await _blob(repo, b"the-shared-base-layer")

        await _manifest(repo, _image(config_a, shared), tag="a")
        await _manifest(repo, _image(config_b, shared), tag="b")

        result = await gc.collect()

        assert result.blobs_deleted == 0
        storage = get_storage()
        assert await storage.exists(keys.oci_blob_key(shared.split(":", 1)[1]))

    async def test_it_is_collected_once_nothing_references_it(self, app) -> None:
        repo = await _repository("team/app")
        config = await _blob(repo, b"config")
        orphan = await _blob(repo, b"nothing-points-at-me")
        await _manifest(repo, _image(config), tag="a")

        result = await gc.collect()

        assert result.blobs_deleted == 1
        storage = get_storage()
        assert not await storage.exists(keys.oci_blob_key(orphan.split(":", 1)[1]))
        # And the referenced one is untouched.
        assert await storage.exists(keys.oci_blob_key(config.split(":", 1)[1]))


class TestPushInFlight:
    """The failure that would corrupt a real push.

    A client uploads every blob and *then* PUTs the manifest. A sweep landing in
    that window sees layers nothing references — because the manifest naming them
    does not exist yet — and deletes them. The push then fails on content the
    registry has just confirmed it holds.
    """

    async def test_a_full_cycle_between_upload_and_manifest_is_survivable(self, app) -> None:
        repo = await _repository("team/app")
        # Uploaded seconds ago, as a real push would be.
        config = await _blob(repo, b"config", age_hours=0)
        layer = await _blob(repo, b"layer", age_hours=0)

        # The sweep runs before the client gets to its manifest PUT.
        result = await gc.collect()
        assert result.blobs_deleted == 0, "a push in flight was collected"

        # ...and the push then completes, which it cannot do if the layers went.
        storage = get_storage()
        for digest in (config, layer):
            assert await storage.exists(keys.oci_blob_key(digest.split(":", 1)[1]))
        await _manifest(repo, _image(config, layer), tag="v1")

        # A later cycle still keeps them, now that they are referenced.
        assert (await gc.collect()).blobs_deleted == 0

    async def test_a_cross_repo_mount_is_protected_by_the_link_not_the_blob(self, app) -> None:
        """An old blob mounted into a new repository is a push in flight too.

        This is why the grace period keys on the age of the link rather than of
        the blob: the layer may have existed for months, but its arrival *here*
        is seconds old and its manifest has not landed.
        """
        source = await _repository("team/source")
        target = await _repository("team/target")

        config = await _blob(source, b"config")
        ancient = await _blob(source, b"a-layer-that-has-existed-for-months")
        await _manifest(source, _image(config, ancient), tag="v1")

        # The blob itself is genuinely old — months, as its name says — and that
        # is what makes this test discriminating. Judged by blob age it looks
        # collectable; judged by the age of its arrival *here* it plainly is not.
        # Without the distinction this test passes for the wrong reason.
        async with get_db_session() as db:
            await db.execute(
                update(OCIBlob)
                .where(OCIBlob.digest == ancient)
                .values(created_at=datetime.now(UTC) - timedelta(days=120))
            )

        # Mounted into the target moments ago; manifest still to come.
        async with get_db_session() as db:
            blob = (await db.execute(select(OCIBlob).where(OCIBlob.digest == ancient))).scalar_one()
            db.add(OCIRepositoryBlob(repository_id=target, blob_id=blob.id))

        assert (await gc.collect()).blobs_deleted == 0

        async with get_db_session() as db:
            links = await db.scalar(
                select(func.count())
                .select_from(OCIRepositoryBlob)
                .where(OCIRepositoryBlob.repository_id == target)
            )
        assert links == 1, "the mount was collected out from under the push"


class TestNothingExpiresByAge:
    """Neither pushed nor mirrored content is deleted for being old.

    A pushed image exists nowhere else. A mirrored one is the copy that keeps
    this cache answering when upstream is unreachable — deleting it on a
    schedule cannot make anything fresher (the next pull asks upstream
    regardless) and throws away the fallback at exactly the wrong moment.

    Freshness is revalidation on access (#1425); this collector only reclaims
    what nothing references.
    """

    async def test_an_old_pushed_image_is_kept(self, app) -> None:
        repo = await _repository("team/app")  # no upstream => pushed
        config = await _blob(repo, b"config")
        await _manifest(repo, _image(config), tag="v1")
        await _age_all_manifests(days=400)

        await gc.collect()

        assert await _blob_count() == 1

    async def test_an_old_mirrored_image_is_kept_too(self, app) -> None:
        """The correction that matters: it is the fallback, not garbage."""
        repo = await _repository("quay.io/ansible/awx-ee", upstream="quay.io")
        config = await _blob(repo, b"config")
        await _manifest(repo, _image(config), tag="latest")
        await _age_all_manifests(days=400)

        await gc.collect()

        assert await _blob_count() == 1

    async def test_blobs_are_freed_once_a_moved_tag_un_references_them(self, app) -> None:
        """How mirrored content is actually reclaimed.

        A tag moving upstream is what leaves the old manifest unreferenced —
        revalidation replaces it, and only then is its content this collector's
        business. Simulated here by removing the superseded manifest, which is
        what repointing the tag does.
        """
        repo = await _repository("quay.io/ansible/awx-ee", upstream="quay.io")
        old_config = await _blob(repo, b"the-old-config")
        new_config = await _blob(repo, b"the-new-config")
        await _manifest(repo, _image(old_config))  # superseded, untagged
        await _manifest(repo, _image(new_config), tag="latest")

        async with get_db_session() as db:
            superseded = (
                (await db.execute(select(OCIManifest).where(OCIManifest.subject_digest.is_(None))))
                .scalars()
                .all()
            )
            for manifest in superseded:
                if manifest.digest != (await _tagged_digest(repo)):
                    await db.delete(manifest)

        result = await gc.collect()

        assert result.blobs_deleted == 1
        assert await _blob_count() == 1


class TestSealed:
    async def test_a_sealed_node_still_only_collects_unreferenced_blobs(
        self, app, monkeypatch
    ) -> None:
        """Sealing changes nothing here, because nothing was expiring anyway.

        It mattered when mirrors expired on a schedule: on a sealed node an
        evicted image could never be re-fetched. Now that nothing is deleted for
        age, the sealed case is simply the ordinary one.
        """
        from terrapod.config import settings

        repo = await _repository("quay.io/ansible/awx-ee", upstream="quay.io")
        config = await _blob(repo, b"config")
        await _blob(repo, b"unreferenced")
        await _manifest(repo, _image(config), tag="latest")
        await _age_all_manifests(days=400)

        monkeypatch.setattr(settings.registry, "cache_only", True)
        result = await gc.collect()

        assert result.blobs_deleted == 1  # the unreferenced one, and only it
        assert await _blob_count() == 1


class TestReachability:
    """What counts as reachable, beyond a tagged image's own layers."""

    async def test_a_signature_is_not_collected_for_lacking_a_tag(self, app) -> None:
        """Referrers — signatures, SBOMs, attestations — carry no tag of their own.

        A naive "untagged means unreachable" sweep destroys provenance while
        leaving the image it describes, which for an air-gapped estate is the
        worst possible half to lose: nothing inside the network can fetch it
        again, and the image still pulls, so the loss is silent.
        """
        repo = await _repository("team/app")
        config = await _blob(repo, b"config")
        subject = await _manifest(repo, _image(config), tag="v1")

        sig_blob = await _blob(repo, b"the-signature-payload")
        await _manifest(
            repo,
            {**_image(sig_blob), "subject": {"digest": subject}},  # no tag
        )

        result = await gc.collect()

        assert result.blobs_deleted == 0
        storage = get_storage()
        assert await storage.exists(keys.oci_blob_key(sig_blob.split(":", 1)[1]))

    async def test_every_architecture_of_a_multi_arch_image_survives(self, app) -> None:
        """An index's entries are manifests, not blobs.

        Walking only `layers` marks the index's own (empty) layer set and nothing
        else, so every architecture's content looks unreferenced — the tag still
        resolves and the image is unusable on all but the one you tested.
        """
        repo = await _repository("team/app")
        amd_config = await _blob(repo, b"amd64-config")
        arm_config = await _blob(repo, b"arm64-config")
        amd = await _manifest(repo, _image(amd_config))
        arm = await _manifest(repo, _image(arm_config))

        await _manifest(
            repo,
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {"digest": amd, "mediaType": "application/vnd.oci.image.manifest.v1+json"},
                    {"digest": arm, "mediaType": "application/vnd.oci.image.manifest.v1+json"},
                ],
            },
            tag="v1",
        )

        result = await gc.collect()

        assert result.blobs_deleted == 0
        storage = get_storage()
        for digest in (amd_config, arm_config):
            assert await storage.exists(keys.oci_blob_key(digest.split(":", 1)[1]))

    async def test_an_unreadable_manifest_collects_nothing_in_that_repository(self, app) -> None:
        """A partial reachability set marks live blobs as garbage.

        If a manifest cannot be read, the only safe answer is to collect nothing
        here — erring toward keeping bytes is the right way for a collector to be
        wrong.
        """
        repo = await _repository("team/app")
        config = await _blob(repo, b"config")
        await _blob(repo, b"genuinely-unreferenced")
        await _manifest(repo, _image(config), tag="v1")

        # The manifest's object goes missing under it.
        async with get_db_session() as db:
            manifest = (await db.execute(select(OCIManifest))).scalar_one()
            key = manifest.storage_key
        await get_storage().delete(key)

        result = await gc.collect()

        assert result.blobs_deleted == 0
        assert result.errors, "an incomplete sweep must say so rather than pass quietly"
