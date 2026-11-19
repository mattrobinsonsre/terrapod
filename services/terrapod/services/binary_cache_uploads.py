"""An operator uploads an engine or platform-tool binary they vetted (#2599).

This is the most consequential upload Terrapod accepts, and the docstring says so
rather than leaving it to be discovered: **the binary is executed on every run**.
The fetch path verifies one against the publisher's signed `SHA256SUMS` before it
writes the row, deliberately fail-closed (#607), and an upload has no signature to
check -- that is the whole point of the posture, not an oversight in it.

What stands in for the signature is PROVENANCE: `attestation: operator-upload`
plus who uploaded it and when. In an audit that is the stronger claim, because it
names a person who vouched rather than asserting a publisher relationship nobody
re-checks. What it must never do is look like a verified fetch, which is why the
row records the attestation rather than quietly filling `shasum` and leaving the
reader to assume it was checked against something.

The rule by which an upload is admitted lives in `cache_admission` and is shared
with the package and provider caches; only the row and key handling are here,
because `CachedBinary` has no size or storage-key column and derives its key from
its coordinates.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import CachedBinary
from terrapod.logging_config import get_logger
from terrapod.services import cache_admission, orphaned_objects
from terrapod.services.binary_cache_service import VALID_TOOLS
from terrapod.services.package_cache import substrate
from terrapod.storage.keys import uploaded_binary_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: Mirrors the package cache: one algorithm, recorded unprefixed because
#: `CachedBinary.shasum` has always held a bare hex sha256 and a prefixed value
#: would silently fail every comparison that reads it.
UPLOAD_ALGORITHM = "sha256"


class BinaryUploadConflict(RuntimeError):
    """An artifact is already at these coordinates and replacement was not asked for."""

    def __init__(self, existing: CachedBinary) -> None:
        who = existing.uploaded_by or "an upstream fetch"
        self.existing = existing
        super().__init__(
            f"{existing.tool} {existing.version} for {existing.os}/{existing.arch} is already "
            f"cached (sha256:{existing.shasum}, admitted by {who}). Pass replace=true to "
            f"overwrite it -- what it displaces may be what a run already depends on."
        )


def validate_coordinates(tool: str, version: str, os_: str, arch: str) -> None:
    """Refuse coordinates this cache cannot address, before any bytes are read."""
    if tool not in VALID_TOOLS:
        raise substrate.CoordinateError(
            f"tool {tool!r} is not one this cache serves; it serves "
            f"{', '.join(sorted(VALID_TOOLS))}"
        )
    for name, value in (("version", version), ("os", os_), ("arch", arch)):
        v = (value or "").strip()
        if not v:
            raise substrate.CoordinateError(f"{name} is required")
        if v != value:
            raise substrate.CoordinateError(f"{name} {value!r} has leading or trailing whitespace")
        if "/" in v or ".." in v:
            raise substrate.CoordinateError(
                f"{name} {value!r} cannot contain '/' or '..' -- it becomes part of a storage key"
            )


async def admit(
    db: AsyncSession,
    storage: ObjectStore,
    *,
    tool: str,
    version: str,
    os_: str,
    arch: str,
    tmp_path: str,
    uploaded_by: str,
    reference: str = "",
    replace: bool = False,
) -> tuple[CachedBinary, cache_admission.Outcome, str]:
    """Admit an uploaded binary, recording who vouched for it.

    Returns the row, what the upload did, and the digest it displaced (empty
    unless it replaced something). `tmp_path` is a file on the ephemeral PVC that
    the caller owns and must unlink.

    The row is flushed before the object is written, for the same reason as the
    package cache: a failed put rolls the row back with the session, where the
    reverse would leave an old row describing new bytes.
    """
    validate_coordinates(tool, version, os_, arch)
    _, hexdigest = await asyncio.to_thread(substrate._digest_file, tmp_path, UPLOAD_ALGORITHM)
    # Under `uploads/`, not beside the pull-through content it stands in for.
    # An upload has no upstream, so it is irreplaceable where cached content is
    # merely cold, and `blob_classes` can only tell them apart by the key --
    # `owning_class` resolves one class per key by longest prefix (#2599).
    key = uploaded_binary_key(tool, version, os_, arch)

    existing = (
        await db.execute(
            select(CachedBinary).where(
                CachedBinary.tool == tool,
                CachedBinary.version == version,
                CachedBinary.os == os_,
                CachedBinary.arch == arch,
            )
        )
    ).scalar_one_or_none()

    outcome = cache_admission.decide(
        occupied=existing is not None,
        existing_is_upload=(
            existing is not None and existing.attestation == substrate.ATTEST_OPERATOR_UPLOAD
        ),
        existing_digest=(existing.shasum or "") if existing is not None else "",
        new_digest=hexdigest,
        replace=replace,
    )

    if outcome is cache_admission.Outcome.CONFLICT:
        assert existing is not None
        raise BinaryUploadConflict(existing)

    if outcome is cache_admission.Outcome.UNCHANGED:
        assert existing is not None
        # Rewrite the object without touching the provenance: this is the only
        # repair for an object that went missing under a row that is otherwise
        # correct, and an uploaded binary has no upstream to re-fetch from.
        await _put(storage, key, tmp_path)
        logger.info(
            "Operator re-uploaded an identical binary",
            tool=tool,
            version=version,
            os=os_,
            arch=arch,
            by=uploaded_by,
        )
        # Also an admission: the operator vouched for these bytes just now.
        # Idempotent on an identical re-upload, and on a REPLACEMENT it is the
        # only record the ledger ever gets of the new digest.
        from terrapod.services.artifact_ledger import coordinate, record_upload

        await record_upload(
            db,
            digest=existing.shasum,
            kind="binary",
            coordinates=coordinate(
                "binary",
                tool=existing.tool,
                version=existing.version,
                os=existing.os,
                arch=existing.arch,
            ),
            uploaded_by=uploaded_by,
            storage_key=existing.storage_key,
        )
        return existing, outcome, ""

    if outcome is cache_admission.Outcome.REPLACE:
        assert existing is not None
        previous = existing.shasum or ""
        now = datetime.now(UTC)
        existing.shasum = hexdigest
        # An uploaded binary has no upstream, and leaving a stale URL would invite
        # a reader to believe one could be re-fetched from it.
        # Nothing points at the old object once the key moves, and every
        # retention sweep is row-driven -- so record it, or it is kept for ever
        # (#2599). Recorded rather than deleted: this transaction can still
        # roll back after the put below succeeds, which would revert the row to
        # the old key, and a deleted object then is data loss rather than a
        # leak.
        if existing.storage_key and existing.storage_key != key:
            await orphaned_objects.record(
                db, existing.storage_key, reason="replaced by an operator upload"
            )
        existing.download_url = ""
        existing.storage_key = key
        existing.attestation = substrate.ATTEST_OPERATOR_UPLOAD
        existing.uploaded_by = uploaded_by
        existing.upload_reference = reference or None
        existing.replaced_digest = previous
        existing.replaced_at = now
        existing.cached_at = now
        existing.last_accessed_at = now
        await db.flush()
        await _put(storage, key, tmp_path)
        # WARNING rather than info: this destroys something, and for a binary that
        # something is what every run on this version executes.
        logger.warning(
            "Operator replaced a cached binary",
            tool=tool,
            version=version,
            os=os_,
            arch=arch,
            digest=hexdigest,
            replaced_digest=previous,
            by=uploaded_by,
        )
        # Also an admission: the operator vouched for these bytes just now.
        # Idempotent on an identical re-upload, and on a REPLACEMENT it is the
        # only record the ledger ever gets of the new digest.
        from terrapod.services.artifact_ledger import coordinate, record_upload

        await record_upload(
            db,
            digest=existing.shasum,
            kind="binary",
            coordinates=coordinate(
                "binary",
                tool=existing.tool,
                version=existing.version,
                os=existing.os,
                arch=existing.arch,
            ),
            uploaded_by=uploaded_by,
            storage_key=existing.storage_key,
        )
        return existing, outcome, previous

    record = CachedBinary(
        tool=tool,
        version=version,
        os=os_,
        arch=arch,
        shasum=hexdigest,
        download_url="",
        storage_key=key,
        attestation=substrate.ATTEST_OPERATOR_UPLOAD,
        uploaded_by=uploaded_by,
        upload_reference=reference or None,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        # Two uploads of the same coordinates at once. The loser re-reads the
        # winner's row and answers as it would have if the winner had got there
        # first, rather than treating a race as licence to overwrite.
        await db.rollback()
        winner = (
            await db.execute(
                select(CachedBinary).where(
                    CachedBinary.tool == tool,
                    CachedBinary.version == version,
                    CachedBinary.os == os_,
                    CachedBinary.arch == arch,
                )
            )
        ).scalar_one_or_none()
        if winner is None:  # pragma: no cover -- the constraint says otherwise
            raise
        raise BinaryUploadConflict(winner) from None

    await _put(storage, key, tmp_path)
    logger.info(
        "Operator uploaded a binary",
        tool=tool,
        version=version,
        os=os_,
        arch=arch,
        digest=hexdigest,
        by=uploaded_by,
    )
    # The ledger is the durable answer to "who vouched for this?" (#2589).
    # Best-effort by construction -- an upload must not fail because the
    # bookkeeping did.
    from terrapod.services.artifact_ledger import coordinate, record_upload

    await record_upload(
        db,
        digest=record.shasum,
        kind="binary",
        coordinates=coordinate(
            "binary", tool=record.tool, version=record.version, os=record.os, arch=record.arch
        ),
        uploaded_by=uploaded_by,
        storage_key=record.storage_key,
    )
    return record, outcome, ""


async def _put(storage: ObjectStore, key: str, tmp_path: str) -> None:
    """Stream the uploaded file into object storage.

    Streamed from disk rather than buffered, exactly as the package cache does it:
    a `tofu` binary is ~100MB and the API pod's heap is not where it goes (rule 14).
    """
    await storage.put_stream(
        key, substrate._file_chunks(tmp_path), content_type="application/octet-stream"
    )
