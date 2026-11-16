"""Serving an uploaded artifact, and re-verifying one (#2599).

Two halves of one requirement the issue states as a requirement rather than a
caveat: a fetched artifact can be re-verified at any time, because the
publisher's material is still upstream; an **uploaded** one cannot be re-verified
against anything at all, so the digest computed on receipt is the whole of its
integrity story. The thing that must not happen is "we trust the operator vetted
it" turning into "we never look at it again".

* `lookup_present` is what the serve path can afford. Every cache read ends in a
  **redirect to object storage**, so the API never sees the bytes a client gets
  and cannot hash them per request; what it can compare is the object's size
  against the row, for one round trip it was making anyway.
* `verify_object` is the full re-hash, on demand.

The refusal's SHAPE is the part worth testing rather than the check itself. An
uploaded artifact whose object has gone must not be reported as a cache miss,
because a miss has two outcomes and both are wrong: unsealed it is filled from
upstream, silently replacing vetted content with unvetted under the coordinates
the operator curated; sealed it deletes the row, discarding the operator's own
record of what should be there.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import CachedPackageFile
from terrapod.services.package_cache import substrate, uploads
from terrapod.storage.protocol import ObjectMeta, ObjectNotFoundError

BODY = b"a vetted wheel, as far as anyone here is concerned" * 50
BODY_SHA = hashlib.sha256(BODY).hexdigest()
KEY = "cache/packages/pypi/flask/flask-3.0.0-py3-none-any.whl"


class _Storage:
    """Only the four methods these paths touch, so a fifth cannot be reached by
    accident and read as working."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.deleted: list[str] = []

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def head(self, key: str) -> ObjectMeta:
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return ObjectMeta(
            key=key,
            size_bytes=len(self.objects[key]),
            content_type="application/octet-stream",
            etag="",
            last_modified=MagicMock(),
        )

    async def get_stream(self, key: str, chunk_size: int = 1024):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        data = self.objects[key]
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


def _row(**kw) -> CachedPackageFile:
    """A real model instance rather than a MagicMock.

    `lookup_present` branches on `attestation` and compares `size`, and a
    MagicMock answers both truthily whatever the code does — so a mock here would
    pass with the branch deleted.
    """
    defaults = {
        "ecosystem": "pypi",
        "name": "flask",
        "version": "3.0.0",
        "filename": "flask-3.0.0-py3-none-any.whl",
        "storage_key": KEY,
        "size": len(BODY),
        "digest": f"sha256:{BODY_SHA}",
        "attestation": substrate.ATTEST_OPERATOR_UPLOAD,
        "uploaded_by": "admin@example.com",
    }
    return CachedPackageFile(**{**defaults, **kw})


def _db(row: CachedPackageFile | None) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute.return_value = result
    db.flush = AsyncMock()
    db.delete = AsyncMock()
    return db


async def _lookup(row, storage):
    return await substrate.lookup_present(
        _db(row), storage, "pypi", "flask", "flask-3.0.0-py3-none-any.whl"
    )


class TestServingAnUploadedArtifact:
    async def test_an_intact_upload_is_served_and_touched(self) -> None:
        row = _row()
        storage = _Storage({KEY: BODY})
        db = _db(row)

        got = await substrate.lookup_present(
            db, storage, "pypi", "flask", "flask-3.0.0-py3-none-any.whl"
        )

        assert got is row
        # Retention is access-based, so an uploaded artifact that is served and
        # not touched is reaped however heavily it is used -- and unlike a
        # fetched one it cannot come back.
        assert db.flush.await_count >= 1

    async def test_a_truncated_object_is_refused_rather_than_served(self) -> None:
        row = _row()
        storage = _Storage({KEY: BODY[:10]})

        with pytest.raises(substrate.UploadedArtifactUnavailable, match="not those bytes"):
            await _lookup(row, storage)

    async def test_a_missing_object_is_refused_rather_than_served(self) -> None:
        row = _row()
        storage = _Storage({})

        with pytest.raises(substrate.UploadedArtifactUnavailable, match="uploaded again"):
            await _lookup(row, storage)

    async def test_the_refusal_names_the_uploader_so_someone_can_be_asked(self) -> None:
        row = _row(uploaded_by="vetting@example.com")
        storage = _Storage({})

        with pytest.raises(substrate.UploadedArtifactUnavailable, match="vetting@example.com"):
            await _lookup(row, storage)

    async def test_a_refused_upload_keeps_its_row(self) -> None:
        """The row is the operator's own record of what should be there.

        Deleting it is what the fetched path does, and it is right there: the
        next request re-fetches. Here there is nothing to re-fetch from, so
        deleting the row would leave nobody able to say what is missing.
        """
        row = _row()
        storage = _Storage({})
        db = _db(row)

        with pytest.raises(substrate.UploadedArtifactUnavailable):
            await substrate.lookup_present(
                db, storage, "pypi", "flask", "flask-3.0.0-py3-none-any.whl"
            )

        assert db.delete.await_count == 0

    async def test_a_refused_upload_never_falls_through_to_a_fetch(self) -> None:
        """`get_or_fetch` must not fill these coordinates from upstream.

        This is the whole reason the refusal is an exception rather than a None.
        A miss here would mean upstream's artifact served under the name the
        operator curated — the posture defeated by its own cache, silently.
        """
        row = _row()
        storage = _Storage({})
        artifact = substrate.Artifact(
            ecosystem="pypi",
            name="flask",
            version="3.0.0",
            filename="flask-3.0.0-py3-none-any.whl",
            upstream_url="https://upstream.test/flask-3.0.0-py3-none-any.whl",
            digest=f"sha256:{BODY_SHA}",
        )

        with pytest.raises(substrate.UploadedArtifactUnavailable):
            await substrate.get_or_fetch(_db(row), storage, artifact)


class TestAFetchedArtifactIsUnchanged:
    """The contrast, which is what proves the branch above is narrow.

    A fetched row with no object is still a miss — deleted and re-fetched — and
    must stay that way: it is re-warmable, which is exactly what an upload is
    not.
    """

    async def test_a_missing_object_on_a_fetched_row_is_still_a_miss(self) -> None:
        row = _row(attestation=substrate.ATTEST_UPSTREAM_DIGEST, uploaded_by=None)
        storage = _Storage({})
        db = _db(row)

        got = await substrate.lookup_present(
            db, storage, "pypi", "flask", "flask-3.0.0-py3-none-any.whl"
        )

        assert got is None
        assert db.delete.await_count == 1

    async def test_a_fetched_row_is_not_size_checked(self) -> None:
        """Deliberately: it would be a check with a worse answer available.

        A fetched artifact whose bytes are wrong is re-verified against
        upstream's own published digest on the next miss, which is strictly
        better than a size comparison — so the size check is for the artifacts
        that have no such recourse.
        """
        row = _row(attestation=substrate.ATTEST_UPSTREAM_DIGEST, uploaded_by=None)
        storage = _Storage({KEY: BODY[:10]})

        got = await _lookup(row, storage)

        assert got is row

    async def test_an_unattested_row_is_still_re_verified(self) -> None:
        """#2589's behaviour, which the upload branch must not have displaced."""
        row = _row(attestation=None, uploaded_by=None)
        storage = _Storage({KEY: BODY})

        assert await _lookup(row, storage) is None


class TestVerifyObject:
    """The full re-hash. Works for a fetched row too — the recorded digest is
    upstream's there, and comparing our stored bytes against it is still the
    right question."""

    async def test_matching_bytes_pass(self) -> None:
        result = await uploads.verify_object(_Storage({KEY: BODY}), _row())

        assert result.ok is True
        assert result.algorithm == "sha256"
        assert result.computed == f"sha256:{BODY_SHA}"
        assert result.stored_size == len(BODY)

    async def test_substituted_bytes_fail_and_report_both_digests(self) -> None:
        other = b"not the artifact that was approved"
        result = await uploads.verify_object(_Storage({KEY: other}), _row())

        assert result.ok is False
        assert result.recorded == f"sha256:{BODY_SHA}"
        assert result.computed == f"sha256:{hashlib.sha256(other).hexdigest()}"

    async def test_bytes_of_the_right_length_but_the_wrong_content_fail(self) -> None:
        """What the serve path's size check cannot see, which is why this exists."""
        same_length = bytes(len(BODY))
        result = await uploads.verify_object(_Storage({KEY: same_length}), _row())

        assert result.ok is False
        assert result.stored_size == result.recorded_size

    async def test_a_missing_object_fails_and_says_it_cannot_be_refetched(self) -> None:
        result = await uploads.verify_object(_Storage({}), _row())

        assert result.ok is False
        assert "uploaded again" in result.detail

    async def test_no_recorded_digest_is_unverifiable_rather_than_passing(self) -> None:
        """A `none-published` row — goproxy and pulumi, whose upstreams publish
        no digest anywhere we can reach. `None` is a third answer, and reporting
        it as a pass would be the strongest possible false claim here.
        """
        row = _row(digest="", attestation=substrate.ATTEST_NONE_PUBLISHED, uploaded_by=None)
        result = await uploads.verify_object(_Storage({KEY: BODY}), row)

        assert result.ok is None
        assert "nothing to verify it against" in result.detail

    async def test_an_unreadable_recorded_digest_is_unverifiable_not_a_failure(self) -> None:
        row = _row(digest="sha256:nothexatall")
        result = await uploads.verify_object(_Storage({KEY: BODY}), row)

        assert result.ok is None
        assert "cannot be read" in result.detail

    async def test_nothing_is_repaired_or_evicted_either_way(self) -> None:
        """What to do about a mismatch is the operator's decision, and for an
        upload there is no re-fetch to make it for them."""
        storage = _Storage({KEY: b"wrong"})
        await uploads.verify_object(storage, _row())

        assert storage.deleted == []
        assert storage.objects == {KEY: b"wrong"}
