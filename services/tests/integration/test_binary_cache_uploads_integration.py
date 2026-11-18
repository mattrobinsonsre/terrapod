"""An operator uploads an engine or platform-tool binary (#2599).

Against real Postgres and real storage, for the reason the package-cache
equivalent gives: `admit` is almost entirely database semantics -- the unique
constraint on `(tool, version, os, arch)` is what makes "already held" a fact
rather than a guess -- and a mocked session can be made to agree with anything.

This is the upload that matters most. A binary is EXECUTED on every run, and the
fetch path verifies one against the publisher's signed SHA256SUMS before writing
its row (#607). An upload has no signature to check, so the thing that must hold
is that it can never be mistaken for a verified fetch.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from terrapod.db.models import CachedBinary
from terrapod.db.session import get_db_session
from terrapod.services import binary_cache_uploads, cache_admission
from terrapod.services.package_cache import substrate
from terrapod.storage import get_storage, keys

pytestmark = pytest.mark.asyncio

VETTED = b"a tofu build an operator compiled and signed off" * 500
VETTED_SHA = hashlib.sha256(VETTED).hexdigest()
REBUILT = b"the same version, rebuilt in-house with a patch" * 500
REBUILT_SHA = hashlib.sha256(REBUILT).hexdigest()

TOOL, VERSION, OS_, ARCH = "tofu", "1.9.0", "linux", "amd64"


@pytest.fixture
def binary_file(tmp_path):
    def make(body: bytes, name: str = "tofu") -> str:
        path = tmp_path / name
        path.write_bytes(body)
        return str(path)

    return make


async def _admit(path: str, *, by: str = "admin@example.com", reference: str = "", replace=False):
    storage = get_storage()
    async with get_db_session() as db:
        result = await binary_cache_uploads.admit(
            db,
            storage,
            tool=TOOL,
            version=VERSION,
            os_=OS_,
            arch=ARCH,
            tmp_path=path,
            uploaded_by=by,
            reference=reference,
            replace=replace,
        )
        await db.commit()
        return result


async def _row() -> CachedBinary | None:
    async with get_db_session() as db:
        return (
            await db.execute(
                select(CachedBinary).where(
                    CachedBinary.tool == TOOL,
                    CachedBinary.version == VERSION,
                    CachedBinary.os == OS_,
                    CachedBinary.arch == ARCH,
                )
            )
        ).scalar_one_or_none()


@pytest.fixture(autouse=True)
async def _clean():
    async with get_db_session() as db:
        for row in (
            await db.execute(select(CachedBinary).where(CachedBinary.tool == TOOL))
        ).scalars():
            await db.delete(row)
        await db.commit()
    yield


class TestAnUploadedBinaryIsRecordedAsOne:
    async def test_the_row_says_an_operator_admitted_it(self, binary_file) -> None:
        """The property the whole posture rests on. A binary is executed on every
        run; if an upload looked like a fetch, the record would assert a publisher
        signature was checked when none was."""
        record, outcome, _ = await _admit(binary_file(VETTED), by="sre@example.com")
        assert outcome is cache_admission.Outcome.FRESH
        assert record.attestation == substrate.ATTEST_OPERATOR_UPLOAD
        assert record.uploaded_by == "sre@example.com"
        assert record.shasum == VETTED_SHA

    async def test_no_download_url_is_left_behind(self, binary_file) -> None:
        """An uploaded binary has no upstream. A stale URL would invite a reader to
        believe it could be re-fetched from there."""
        record, _, _ = await _admit(binary_file(VETTED))
        assert record.download_url == ""

    async def test_the_bytes_land_where_the_serve_path_looks(self, binary_file) -> None:
        record, _, _ = await _admit(binary_file(VETTED))
        storage = get_storage()
        # Where the serve path looks is the row's recorded key, not a derived
        # one: an upload lives under `uploads/` so replication can tier it as
        # irreplaceable rather than as a re-warmable cache (#2599).
        assert record.storage_key == keys.uploaded_binary_key(TOOL, VERSION, OS_, ARCH)
        stored = b"".join([chunk async for chunk in storage.get_stream(record.storage_key)])
        assert hashlib.sha256(stored).hexdigest() == VETTED_SHA
        # And deriving would aim at a different object: on a node that had
        # already pulled this binary, the derived key still holds the OLD
        # upstream bytes. This assertion is what caught the derived form -- it
        # passed locally on bytes an earlier run had left at that key, and
        # failed in CI against a clean store.
        assert record.storage_key != keys.binary_cache_key(TOOL, VERSION, OS_, ARCH)

    async def test_the_reference_is_stored_and_never_interpreted(self, binary_file) -> None:
        record, _, _ = await _admit(binary_file(VETTED), reference="CHG-4471 / approved by sec")
        assert record.upload_reference == "CHG-4471 / approved by sec"


class TestReplacementIsDeliberate:
    async def test_different_bytes_are_refused_without_replace(self, binary_file) -> None:
        await _admit(binary_file(VETTED))
        with pytest.raises(binary_cache_uploads.BinaryUploadConflict):
            await _admit(binary_file(REBUILT))

    async def test_the_refusal_names_what_is_already_there(self, binary_file) -> None:
        await _admit(binary_file(VETTED), by="first@example.com")
        with pytest.raises(binary_cache_uploads.BinaryUploadConflict) as exc:
            await _admit(binary_file(REBUILT))
        detail = str(exc.value)
        assert VETTED_SHA in detail
        assert "first@example.com" in detail
        assert "replace=true" in detail

    async def test_a_refused_upload_changes_nothing(self, binary_file) -> None:
        await _admit(binary_file(VETTED), by="first@example.com")
        with pytest.raises(binary_cache_uploads.BinaryUploadConflict):
            await _admit(binary_file(REBUILT), by="second@example.com")
        row = await _row()
        assert row is not None
        assert row.shasum == VETTED_SHA
        assert row.uploaded_by == "first@example.com"

    async def test_a_replacement_records_what_it_displaced(self, binary_file) -> None:
        """Without the previous digest that fact is gone the instant the row is
        rewritten -- and for a binary, what was displaced is what every run on this
        version was executing."""
        await _admit(binary_file(VETTED))
        record, outcome, previous = await _admit(binary_file(REBUILT), replace=True)
        assert outcome is cache_admission.Outcome.REPLACE
        assert previous == VETTED_SHA
        assert record.shasum == REBUILT_SHA
        assert record.replaced_digest == VETTED_SHA
        assert record.replaced_at is not None

    async def test_identical_bytes_are_not_a_replacement(self, binary_file) -> None:
        """Re-uploading the same binary is an operator re-asserting it, and is the
        only repair for an object that went missing under a correct row -- there is
        no upstream to re-fetch an uploaded binary from."""
        first, _, _ = await _admit(binary_file(VETTED), by="first@example.com")
        cached_at = first.cached_at
        record, outcome, _ = await _admit(binary_file(VETTED), by="second@example.com")
        assert outcome is cache_admission.Outcome.UNCHANGED
        # The provenance is the FIRST assertion of these bytes, not the latest.
        assert record.uploaded_by == "first@example.com"
        assert record.cached_at == cached_at


class TestCoordinatesAreRefusedBeforeAnyBytesAreRead:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"tool": "definitely-not-a-tool"},
            {"version": ""},
            {"version": "../escape"},
            {"os_": "linux/x"},
            {"arch": " amd64"},
        ],
    )
    async def test_a_coordinate_this_cache_cannot_address_is_refused(self, kwargs) -> None:
        base = {"tool": TOOL, "version": VERSION, "os_": OS_, "arch": ARCH}
        base.update(kwargs)
        with pytest.raises(substrate.CoordinateError):
            binary_cache_uploads.validate_coordinates(
                base["tool"], base["version"], base["os_"], base["arch"]
            )
