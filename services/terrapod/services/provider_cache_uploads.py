"""An operator uploads a provider archive they vetted (#2599).

The second of the two kinds a Terraform run cannot proceed without. A sealed
deployment given `tofu` and no providers still cannot plan anything.

What makes this different from the binary upload is the **h1 hash**. For a binary,
the digest is bookkeeping. For a provider it is the thing the CONSUMER checks: the
`h1:` entries in a workspace's `.terraform.lock.hcl` are what `terraform init`
compares a downloaded provider against, so an uploaded archive whose recorded h1
does not match its bytes is refused by the engine, not by us. It is therefore
computed from the uploaded zip rather than accepted from the uploader -- a field
an operator could fill in by hand is one they can fill in wrongly, and the failure
would surface as an unexplained `init` error on someone else's workspace.

Computing it also validates the archive: h1 is defined over a zip's entries, so a
file that is not a readable zip cannot produce one and is refused before anything
is stored.

The rule by which an upload is admitted lives in `cache_admission` and is shared
with the package and binary caches; only the row and key handling are here.
"""

from __future__ import annotations

import asyncio
import zipfile
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import CachedProviderPackage
from terrapod.logging_config import get_logger
from terrapod.services import cache_admission, orphaned_objects
from terrapod.services.package_cache import substrate
from terrapod.storage.keys import uploaded_provider_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

UPLOAD_ALGORITHM = "sha256"


class ProviderUploadConflict(RuntimeError):
    """An archive is already at these coordinates and replacement was not asked for."""

    def __init__(self, existing: CachedProviderPackage) -> None:
        who = existing.uploaded_by or "an upstream fetch"
        self.existing = existing
        super().__init__(
            f"{existing.hostname}/{existing.namespace}/{existing.type} {existing.version} for "
            f"{existing.os}/{existing.arch} is already cached (sha256:{existing.shasum}, "
            f"admitted by {who}). Pass replace=true to overwrite it -- what it displaces may "
            f"be what a run already depends on."
        )


class NotAProviderArchive(RuntimeError):
    """The uploaded bytes are not a readable zip, so no h1 can be computed."""


def validate_coordinates(
    hostname: str, namespace: str, type_: str, version: str, os_: str, arch: str
) -> None:
    """Refuse coordinates this cache cannot address, before any bytes are read."""
    for name, value in (
        ("hostname", hostname),
        ("namespace", namespace),
        ("type", type_),
        ("version", version),
        ("os", os_),
        ("arch", arch),
    ):
        v = (value or "").strip()
        if not v:
            raise substrate.CoordinateError(f"{name} is required")
        if v != value:
            raise substrate.CoordinateError(f"{name} {value!r} has leading or trailing whitespace")
        if "/" in v or ".." in v:
            raise substrate.CoordinateError(
                f"{name} {value!r} cannot contain '/' or '..' -- it becomes part of a storage key"
            )


def archive_filename(type_: str, version: str, os_: str, arch: str) -> str:
    """The name the registry protocol gives a provider archive.

    Derived rather than taken from the uploader: the download protocol serves this
    exact name, so an arbitrary one would be stored somewhere `init` never looks.
    """
    return f"terraform-provider-{type_}_{version}_{os_}_{arch}.zip"


def _h1_and_digest(path: str) -> tuple[str, str]:
    """(h1, sha256) for a provider zip on disk, in constant memory.

    Both come from the bytes rather than from the uploader. The h1 is what
    `terraform init` checks a provider against, so a value supplied by hand would
    surface as an unexplained `init` failure on someone else's workspace.
    """
    from terrapod.services.provider_cache_service import _compute_h1_from_zip_path

    try:
        h1 = _compute_h1_from_zip_path(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise NotAProviderArchive(
            "the uploaded file is not a readable zip, so no h1 hash can be computed for it. "
            "A provider archive is the publisher's .zip; `terraform init` verifies it against "
            "the h1 entries in a workspace's .terraform.lock.hcl."
        ) from exc
    _, hexdigest = substrate._digest_file(path, UPLOAD_ALGORITHM)
    return h1, hexdigest


async def admit(
    db: AsyncSession,
    storage: ObjectStore,
    *,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
    tmp_path: str,
    uploaded_by: str,
    reference: str = "",
    replace: bool = False,
) -> tuple[CachedProviderPackage, cache_admission.Outcome, str]:
    """Admit an uploaded provider archive, recording who vouched for it."""
    validate_coordinates(hostname, namespace, type_, version, os_, arch)
    h1, hexdigest = await asyncio.to_thread(_h1_and_digest, tmp_path)
    filename = archive_filename(type_, version, os_, arch)
    # Under `uploads/`, not beside the pull-through content it stands in for.
    # An upload has no upstream, so it is irreplaceable where cached content is
    # merely cold, and `blob_classes` can only tell them apart by the key --
    # `owning_class` resolves one class per key by longest prefix (#2599).
    key = uploaded_provider_key(hostname, namespace, type_, version, filename)

    existing = (
        await db.execute(
            select(CachedProviderPackage).where(
                CachedProviderPackage.hostname == hostname,
                CachedProviderPackage.namespace == namespace,
                CachedProviderPackage.type == type_,
                CachedProviderPackage.version == version,
                CachedProviderPackage.os == os_,
                CachedProviderPackage.arch == arch,
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
        raise ProviderUploadConflict(existing)

    if outcome is cache_admission.Outcome.UNCHANGED:
        assert existing is not None
        await _put(storage, key, tmp_path)
        logger.info(
            "Operator re-uploaded an identical provider archive",
            hostname=hostname,
            namespace=namespace,
            type=type_,
            version=version,
            by=uploaded_by,
        )
        # Also an admission: the operator vouched for these bytes just now.
        # Idempotent on an identical re-upload, and on a REPLACEMENT it is the
        # only record the ledger ever gets of the new digest.
        from terrapod.services.artifact_ledger import coordinate, record_upload

        await record_upload(
            db,
            digest=existing.shasum,
            kind="provider",
            coordinates=coordinate(
                "provider",
                hostname=existing.hostname,
                namespace=existing.namespace,
                type=existing.type,
                version=existing.version,
                filename=existing.filename,
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
        existing.h1_hash = h1
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
        existing.filename = filename
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
        logger.warning(
            "Operator replaced a cached provider archive",
            hostname=hostname,
            namespace=namespace,
            type=type_,
            version=version,
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
            kind="provider",
            coordinates=coordinate(
                "provider",
                hostname=existing.hostname,
                namespace=existing.namespace,
                type=existing.type,
                version=existing.version,
                filename=existing.filename,
            ),
            uploaded_by=uploaded_by,
            storage_key=existing.storage_key,
        )
        return existing, outcome, previous

    record = CachedProviderPackage(
        hostname=hostname,
        namespace=namespace,
        type=type_,
        version=version,
        os=os_,
        arch=arch,
        filename=filename,
        shasum=hexdigest,
        h1_hash=h1,
        storage_key=key,
        attestation=substrate.ATTEST_OPERATOR_UPLOAD,
        uploaded_by=uploaded_by,
        upload_reference=reference or None,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        winner = (
            await db.execute(
                select(CachedProviderPackage).where(
                    CachedProviderPackage.hostname == hostname,
                    CachedProviderPackage.namespace == namespace,
                    CachedProviderPackage.type == type_,
                    CachedProviderPackage.version == version,
                    CachedProviderPackage.os == os_,
                    CachedProviderPackage.arch == arch,
                )
            )
        ).scalar_one_or_none()
        if winner is None:  # pragma: no cover -- the constraint says otherwise
            raise
        raise ProviderUploadConflict(winner) from None

    await _put(storage, key, tmp_path)
    logger.info(
        "Operator uploaded a provider archive",
        hostname=hostname,
        namespace=namespace,
        type=type_,
        version=version,
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
        kind="provider",
        coordinates=coordinate(
            "provider",
            hostname=record.hostname,
            namespace=record.namespace,
            type=record.type,
            version=record.version,
            filename=record.filename,
        ),
        uploaded_by=uploaded_by,
        storage_key=record.storage_key,
    )
    return record, outcome, ""


async def _put(storage: ObjectStore, key: str, tmp_path: str) -> None:
    """Stream the uploaded archive into object storage (rule 14: never buffered)."""
    await storage.put_stream(key, substrate._file_chunks(tmp_path), content_type="application/zip")
