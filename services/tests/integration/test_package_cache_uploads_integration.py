"""Operator uploads against real Postgres and real storage (#2599).

`admit` is almost entirely database semantics — the unique constraint on
`(ecosystem, name, filename)` is what makes "already held" a fact rather than a
guess, and the race between two admins uploading at once resolves through that
constraint. A mocked session can be made to agree with any of it, so the
behaviour belongs here.

The three properties that are the feature, rather than its plumbing:

* an uploaded artifact is SERVED, with the pull-through never consulted — which
  is the posture working;
* an upload can never be mistaken for a fetch in the record;
* a replacement has to be asked for, and what it displaced is recorded.
"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest
from sqlalchemy import func, select

from terrapod.db.models import CachedPackageFile
from terrapod.db.session import get_db_session
from terrapod.services.package_cache import pypi, substrate, uploads
from terrapod.storage import get_storage, keys

pytestmark = pytest.mark.asyncio

VETTED = b"a wheel an operator unpacked, read and approved" * 200
VETTED_SHA = hashlib.sha256(VETTED).hexdigest()
REBUILT = b"the same package, rebuilt in-house with a patch" * 200
REBUILT_SHA = hashlib.sha256(REBUILT).hexdigest()

PROJECT = "flask"
FILENAME = "flask-3.0.0-py3-none-any.whl"
COORDINATES = pypi.upload_coordinates(PROJECT, FILENAME)


@pytest.fixture
def artifact_file(tmp_path):
    """A factory for the file `admit` takes, on disk as a real upload is."""

    def make(body: bytes, name: str = "upload.bin") -> str:
        path = tmp_path / name
        path.write_bytes(body)
        return str(path)

    return make


async def _admit(path: str, *, by: str = "admin@example.com", reference: str = "", replace=False):
    storage = get_storage()
    async with get_db_session() as db:
        return await uploads.admit(
            db,
            storage,
            ecosystem=pypi.ECOSYSTEM,
            coordinates=COORDINATES,
            tmp_path=path,
            uploaded_by=by,
            reference=reference,
            replace=replace,
        )


async def _row() -> CachedPackageFile | None:
    async with get_db_session() as db:
        return await substrate.lookup(db, pypi.ECOSYSTEM, COORDINATES.name, FILENAME)


class TestAFirstUpload:
    async def test_the_row_and_the_object_both_land(self, app, artifact_file) -> None:
        storage = get_storage()

        admission = await _admit(artifact_file(VETTED), reference="CHG-4471")

        assert admission.replaced is False
        assert admission.unchanged is False
        row = await _row()
        assert row is not None
        assert row.size == len(VETTED)
        assert await storage.get(row.storage_key) == VETTED

    async def test_the_digest_is_computed_on_receipt(self, app, artifact_file) -> None:
        """Nothing published one, so this is the artifact's identity.

        Recorded in the same column and the same form a fetched row records
        upstream's in, so every surface that already shows a digest shows this
        one -- `attestation` is what says whose it is.
        """
        await _admit(artifact_file(VETTED))

        row = await _row()
        assert row.digest == f"sha256:{VETTED_SHA}"

    async def test_the_record_says_uploaded_by(self, app, artifact_file) -> None:
        """Requirement 1 on the issue, and the whole audit trail.

        An uploaded artifact that read as fetched-and-verified would make the
        ledger lie about the most important thing on it.
        """
        await _admit(artifact_file(VETTED), by="vetting@example.com", reference="CHG-4471")

        row = await _row()
        assert row.attestation == substrate.ATTEST_OPERATOR_UPLOAD
        assert row.attestation != substrate.ATTEST_UPSTREAM_DIGEST
        assert row.uploaded_by == "vetting@example.com"
        assert row.upload_reference == "CHG-4471"
        assert row.replaced_digest is None
        assert row.replaced_at is None

    async def test_an_omitted_reference_is_null_rather_than_empty(self, app, artifact_file) -> None:
        """An operator who runs no ticketing process is still the attestor, and
        "no reference given" must not read as a reference that is blank."""
        await _admit(artifact_file(VETTED))

        assert (await _row()).upload_reference is None

    async def test_it_lands_on_the_key_the_serve_path_asks_for(self, app, artifact_file) -> None:
        await _admit(artifact_file(VETTED))

        row = await _row()
        # Under `uploads/`, and the serve path finds it because it reads the
        # key off the row rather than deriving one (#2599). An upload has no
        # upstream, so it is irreplaceable where pull-through content is merely
        # cold -- and `blob_classes.owning_class` resolves exactly one class per
        # key by longest prefix, so the two cannot share `cache/packages/`.
        assert row.storage_key == keys.uploaded_package_key(pypi.ECOSYSTEM, "flask", FILENAME)
        # The discriminating half: it is NOT where the pull-through cache would
        # have put it. Without this the assertion above would pass if the two
        # key builders ever converged.
        assert row.storage_key != keys.package_cache_key(pypi.ECOSYSTEM, "flask", FILENAME)


class TestTheUploadedArtifactIsWhatGetsServed:
    async def test_get_or_fetch_returns_it_without_reaching_upstream(
        self, app, artifact_file
    ) -> None:
        """The posture, in one test.

        A client given a transport that raises is the assertion: if the cache
        went upstream for an artifact an operator had already supplied, the
        deployment is not serving only what was vetted.
        """

        def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError(f"went upstream for {request.url}")

        await _admit(artifact_file(VETTED))

        storage = get_storage()
        artifact = substrate.Artifact(
            ecosystem=pypi.ECOSYSTEM,
            name="flask",
            version="3.0.0",
            filename=FILENAME,
            upstream_url=f"https://upstream.test/{FILENAME}",
            digest=f"sha256:{VETTED_SHA}",
        )
        async with (
            get_db_session() as db,
            httpx.AsyncClient(transport=httpx.MockTransport(explode)) as client,
        ):
            served = await substrate.get_or_fetch(db, storage, artifact, client=client)

        assert served.attestation == substrate.ATTEST_OPERATOR_UPLOAD
        assert served.digest == f"sha256:{VETTED_SHA}"

    async def test_an_upload_works_while_the_node_is_sealed(
        self, app, artifact_file, monkeypatch
    ) -> None:
        """The entire reason this exists.

        A sealed deployment has no other way to get an artifact in: every other
        write to this cache is an upstream fetch, and `fetch_and_cache` refuses
        outright when `cache_only` is set. An upload that was refused too would
        leave the posture unreachable.
        """
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry, "cache_only", True)
        assert substrate.sealed()

        await _admit(artifact_file(VETTED))

        assert (await _row()).digest == f"sha256:{VETTED_SHA}"


class TestReplacementIsDeliberate:
    async def test_a_repeat_upload_of_different_bytes_is_refused(self, app, artifact_file) -> None:
        """Requirement 4. What is being overwritten may be exactly what a run
        already depends on, so it is never silent."""
        await _admit(artifact_file(VETTED))

        with pytest.raises(uploads.UploadConflict) as exc:
            await _admit(artifact_file(REBUILT, "rebuilt.bin"))

        assert "replace=true" in str(exc.value)

    async def test_a_refused_upload_changes_nothing(self, app, artifact_file) -> None:
        storage = get_storage()
        await _admit(artifact_file(VETTED), by="first@example.com")

        with pytest.raises(uploads.UploadConflict):
            await _admit(artifact_file(REBUILT, "rebuilt.bin"), by="second@example.com")

        row = await _row()
        assert row.digest == f"sha256:{VETTED_SHA}"
        assert row.uploaded_by == "first@example.com"
        assert await storage.get(row.storage_key) == VETTED

    async def test_the_refusal_names_what_is_already_there(self, app, artifact_file) -> None:
        """A 409 that only said "conflict" would send an operator to go and look
        up the thing the server already had in its hand."""
        await _admit(artifact_file(VETTED))

        with pytest.raises(uploads.UploadConflict) as exc:
            await _admit(artifact_file(REBUILT, "rebuilt.bin"))

        assert VETTED_SHA in str(exc.value)
        assert substrate.ATTEST_OPERATOR_UPLOAD in str(exc.value)

    async def test_replace_rewrites_the_row_and_the_object(self, app, artifact_file) -> None:
        storage = get_storage()
        await _admit(artifact_file(VETTED))

        admission = await _admit(artifact_file(REBUILT, "rebuilt.bin"), replace=True)

        assert admission.replaced is True
        row = await _row()
        assert row.digest == f"sha256:{REBUILT_SHA}"
        assert row.size == len(REBUILT)
        assert await storage.get(row.storage_key) == REBUILT

    async def test_what_was_displaced_is_recorded(self, app, artifact_file) -> None:
        """Without the previous digest that fact is gone the instant the row is
        rewritten, which is the silent overwrite the requirement forbids."""
        await _admit(artifact_file(VETTED), by="first@example.com")

        await _admit(artifact_file(REBUILT, "rebuilt.bin"), by="second@example.com", replace=True)

        row = await _row()
        assert row.replaced_digest == f"sha256:{VETTED_SHA}"
        assert row.replaced_at is not None
        assert row.uploaded_by == "second@example.com"

    async def test_it_is_still_one_row(self, app, artifact_file) -> None:
        """A replacement rewrites the artifact's record rather than adding a
        second one at the same coordinates."""
        await _admit(artifact_file(VETTED))
        await _admit(artifact_file(REBUILT, "rebuilt.bin"), replace=True)

        async with get_db_session() as db:
            count = await db.scalar(select(func.count()).select_from(CachedPackageFile))
        assert count == 1


class TestReAdmittingTheIdenticalArtifact:
    async def test_the_same_bytes_again_are_not_a_conflict(self, app, artifact_file) -> None:
        """The operator is re-asserting what is already recorded, not replacing
        anything, so refusing it would be refusing a no-op."""
        await _admit(artifact_file(VETTED))

        admission = await _admit(artifact_file(VETTED, "again.bin"))

        assert admission.unchanged is True
        assert admission.replaced is False

    async def test_it_leaves_the_first_assertion_standing(self, app, artifact_file) -> None:
        """`uploaded_by` answers "who put these bytes here", and that is the
        first person who did -- not the last person to re-upload them."""
        await _admit(artifact_file(VETTED), by="first@example.com", reference="CHG-1")

        await _admit(artifact_file(VETTED, "again.bin"), by="second@example.com", reference="CHG-2")

        row = await _row()
        assert row.uploaded_by == "first@example.com"
        assert row.upload_reference == "CHG-1"
        assert row.replaced_digest is None

    async def test_it_repairs_an_object_that_went_missing(self, app, artifact_file) -> None:
        """The one thing that can fix an uploaded artifact with no upstream.

        The serve path refuses a row whose object has gone, and nothing can
        re-fetch it -- so re-uploading the identical bytes has to put them back
        rather than being short-circuited as a no-op.
        """
        storage = get_storage()
        await _admit(artifact_file(VETTED))
        row = await _row()
        await storage.delete(row.storage_key)
        assert not await storage.exists(row.storage_key)

        await _admit(artifact_file(VETTED, "again.bin"))

        assert await storage.get(row.storage_key) == VETTED


class TestConvertingAFetchedArtifactIntoAVettedOne:
    async def test_replacing_a_fetched_row_makes_it_an_upload(self, app, artifact_file) -> None:
        """What sealing a deployment with an existing warm cache looks like.

        The row is already there from a pull-through fetch; the operator uploads
        the artifact they vetted over it, and from then on the record says so.
        `replace` is required because the fetched bytes are what runs have been
        using.
        """
        storage = get_storage()
        async with get_db_session() as db:
            db.add(
                CachedPackageFile(
                    ecosystem=pypi.ECOSYSTEM,
                    name="flask",
                    version="3.0.0",
                    filename=FILENAME,
                    storage_key=keys.package_cache_key(pypi.ECOSYSTEM, "flask", FILENAME),
                    size=len(VETTED),
                    digest=f"sha256:{VETTED_SHA}",
                    attestation=substrate.ATTEST_UPSTREAM_DIGEST,
                )
            )
        await storage.put(keys.package_cache_key(pypi.ECOSYSTEM, "flask", FILENAME), VETTED)

        with pytest.raises(uploads.UploadConflict):
            await _admit(artifact_file(REBUILT, "rebuilt.bin"))
        await _admit(artifact_file(REBUILT, "rebuilt.bin"), by="vetting@example.com", replace=True)

        row = await _row()
        assert row.attestation == substrate.ATTEST_OPERATOR_UPLOAD
        assert row.uploaded_by == "vetting@example.com"
        assert row.replaced_digest == f"sha256:{VETTED_SHA}"

    async def test_identical_bytes_over_a_fetched_row_still_need_replace(
        self, app, artifact_file
    ) -> None:
        """The no-conflict fast path is for an upload over an UPLOAD.

        A fetched row with the same bytes is still a different assertion -- it
        says upstream published these, not that anyone here vetted them -- so
        converting it is a deliberate act like any other replacement.
        """
        async with get_db_session() as db:
            db.add(
                CachedPackageFile(
                    ecosystem=pypi.ECOSYSTEM,
                    name="flask",
                    version="3.0.0",
                    filename=FILENAME,
                    storage_key=keys.package_cache_key(pypi.ECOSYSTEM, "flask", FILENAME),
                    size=len(VETTED),
                    digest=f"sha256:{VETTED_SHA}",
                    attestation=substrate.ATTEST_UPSTREAM_DIGEST,
                )
            )

        with pytest.raises(uploads.UploadConflict):
            await _admit(artifact_file(VETTED))


class TestTwoAdminsAtOnce:
    async def test_the_loser_conflicts_rather_than_overwriting_or_500ing(
        self, app, artifact_file
    ) -> None:
        """A race is not licence to overwrite.

        Whichever path the two take -- one losing the lookup and hitting the
        unique constraint, or the second simply finding the first's committed row
        -- the answer is the same: one artifact admitted, one honest 409, and no
        raw IntegrityError reaching the caller.
        """
        first = artifact_file(VETTED, "a.bin")
        second = artifact_file(REBUILT, "b.bin")

        outcomes = await asyncio.gather(
            _admit(first, by="a@example.com"),
            _admit(second, by="b@example.com"),
            return_exceptions=True,
        )

        admitted = [o for o in outcomes if isinstance(o, uploads.Admission)]
        refused = [o for o in outcomes if isinstance(o, uploads.UploadConflict)]
        other = [
            o for o in outcomes if not isinstance(o, uploads.Admission | uploads.UploadConflict)
        ]
        assert other == [], other
        assert len(admitted) == 1
        assert len(refused) == 1

        async with get_db_session() as db:
            count = await db.scalar(select(func.count()).select_from(CachedPackageFile))
        assert count == 1
