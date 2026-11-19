"""An operator uploads a provider archive (#2599).

The other half of what a sealed deployment needs: given `tofu` and no providers, a
run still cannot plan anything.

What distinguishes this from the binary upload is the **h1 hash**. For a binary
the digest is bookkeeping; for a provider it is what the CONSUMER checks -- the
`h1:` entries in a workspace's `.terraform.lock.hcl` are what `terraform init`
compares a downloaded provider against. So it is computed from the uploaded zip
rather than accepted from the uploader, and the tests pin that it matches what a
fetched archive would have recorded.
"""

from __future__ import annotations

import hashlib
import zipfile

import pytest
from sqlalchemy import select

from terrapod.db.models import CachedProviderPackage
from terrapod.db.session import get_db_session
from terrapod.services import cache_admission, provider_cache_uploads
from terrapod.services.package_cache import substrate
from terrapod.storage import get_storage, keys

pytestmark = pytest.mark.asyncio

HOST, NS, TYPE, VERSION, OS_, ARCH = (
    "registry.terraform.io",
    "hashicorp",
    "null",
    "3.2.1",
    "linux",
    "amd64",
)


def _zip_bytes(payload: bytes) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"terraform-provider-{TYPE}_v{VERSION}", payload)
        z.writestr("LICENSE.txt", b"MPL-2.0")
    return buf.getvalue()


VETTED = _zip_bytes(b"a provider an operator reviewed" * 400)
REBUILT = _zip_bytes(b"the same version, rebuilt in-house" * 400)
VETTED_SHA = hashlib.sha256(VETTED).hexdigest()


@pytest.fixture
def archive(tmp_path):
    def make(body: bytes, name: str = "provider.zip") -> str:
        path = tmp_path / name
        path.write_bytes(body)
        return str(path)

    return make


async def _admit(path: str, *, by: str = "admin@example.com", reference: str = "", replace=False):
    storage = get_storage()
    async with get_db_session() as db:
        result = await provider_cache_uploads.admit(
            db,
            storage,
            hostname=HOST,
            namespace=NS,
            type_=TYPE,
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


async def _row() -> CachedProviderPackage | None:
    async with get_db_session() as db:
        return (
            await db.execute(
                select(CachedProviderPackage).where(
                    CachedProviderPackage.hostname == HOST,
                    CachedProviderPackage.namespace == NS,
                    CachedProviderPackage.type == TYPE,
                    CachedProviderPackage.version == VERSION,
                )
            )
        ).scalar_one_or_none()


@pytest.fixture(autouse=True)
async def _clean():
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(CachedProviderPackage).where(CachedProviderPackage.hostname == HOST)
            )
        ).scalars()
        for row in rows:
            await db.delete(row)
        await db.commit()
    yield


class TestTheH1IsComputedFromTheBytes:
    async def test_it_matches_what_a_fetch_would_have_recorded(self, archive) -> None:
        """The property the whole thing rests on. `terraform init` checks a
        provider against the h1 in a workspace's lockfile, so an uploaded archive
        and a fetched one of the same bytes must record the same value -- otherwise
        an upload breaks `init` for everyone already pinning that provider."""
        from terrapod.services.provider_cache_service import _compute_h1_from_zip_bytes

        record, _, _ = await _admit(archive(VETTED))
        assert record.h1_hash == _compute_h1_from_zip_bytes(VETTED)
        assert record.h1_hash.startswith("h1:")

    async def test_a_file_that_is_not_a_zip_is_refused(self, archive) -> None:
        """Computing the h1 validates the archive: there is no h1 for a non-zip, so
        it is refused before anything is stored rather than cached as a provider
        nothing can install."""
        with pytest.raises(provider_cache_uploads.NotAProviderArchive):
            await _admit(archive(b"this is not a zip at all"))
        assert await _row() is None

    async def test_the_filename_is_derived_not_supplied(self, archive) -> None:
        """The download protocol serves this exact name; an arbitrary one would be
        stored where `init` never looks."""
        record, _, _ = await _admit(archive(VETTED))
        assert record.filename == f"terraform-provider-{TYPE}_{VERSION}_{OS_}_{ARCH}.zip"


class TestAnUploadedProviderIsRecordedAsOne:
    async def test_the_row_says_an_operator_admitted_it(self, archive) -> None:
        record, outcome, _ = await _admit(archive(VETTED), by="sre@example.com")
        assert outcome is cache_admission.Outcome.FRESH
        assert record.attestation == substrate.ATTEST_OPERATOR_UPLOAD
        assert record.uploaded_by == "sre@example.com"
        assert record.shasum == VETTED_SHA

    async def test_the_bytes_land_where_the_serve_path_looks(self, archive) -> None:
        record, _, _ = await _admit(archive(VETTED))
        storage = get_storage()
        # Where the serve path looks is the row's recorded key, not a derived
        # one -- an upload lives under `uploads/` so that `blob_classes` can
        # tier it as irreplaceable rather than as a re-warmable cache (#2599).
        assert record.storage_key == keys.uploaded_provider_key(
            HOST, NS, TYPE, VERSION, record.filename
        )
        stored = b"".join([chunk async for chunk in storage.get_stream(record.storage_key)])
        assert hashlib.sha256(stored).hexdigest() == VETTED_SHA
        # And deriving would have aimed at a different object entirely, which is
        # the failure this records: on a workspace that had already pulled this
        # provider, the derived key still holds the OLD upstream bytes.
        assert record.storage_key != keys.provider_cache_key(
            HOST, NS, TYPE, VERSION, record.filename
        )


class TestReplacementIsDeliberate:
    async def test_different_bytes_are_refused_without_replace(self, archive) -> None:
        await _admit(archive(VETTED))
        with pytest.raises(provider_cache_uploads.ProviderUploadConflict):
            await _admit(archive(REBUILT))

    async def test_a_refused_upload_changes_nothing(self, archive) -> None:
        await _admit(archive(VETTED), by="first@example.com")
        with pytest.raises(provider_cache_uploads.ProviderUploadConflict):
            await _admit(archive(REBUILT), by="second@example.com")
        row = await _row()
        assert row is not None
        assert row.shasum == VETTED_SHA
        assert row.uploaded_by == "first@example.com"

    async def test_a_replacement_records_what_it_displaced(self, archive) -> None:
        await _admit(archive(VETTED))
        record, outcome, previous = await _admit(archive(REBUILT), replace=True)
        assert outcome is cache_admission.Outcome.REPLACE
        assert previous == VETTED_SHA
        assert record.replaced_digest == VETTED_SHA
        assert record.replaced_at is not None

    async def test_a_replacement_recomputes_the_h1(self, archive) -> None:
        """A stale h1 after a replacement is worse than no h1: `init` would refuse
        the new bytes against the old recorded value."""
        from terrapod.services.provider_cache_service import _compute_h1_from_zip_bytes

        await _admit(archive(VETTED))
        record, _, _ = await _admit(archive(REBUILT), replace=True)
        assert record.h1_hash == _compute_h1_from_zip_bytes(REBUILT)

    async def test_identical_bytes_are_not_a_replacement(self, archive) -> None:
        first, _, _ = await _admit(archive(VETTED), by="first@example.com")
        cached_at = first.cached_at
        record, outcome, _ = await _admit(archive(VETTED), by="second@example.com")
        assert outcome is cache_admission.Outcome.UNCHANGED
        assert record.uploaded_by == "first@example.com"
        assert record.cached_at == cached_at
