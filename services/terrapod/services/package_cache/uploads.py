"""An operator uploads an artifact they have vetted themselves (#2599).

The third posture after verification and scanning (#2589), for a deployment whose
rule is "nothing runs here that we have not vetted ourselves". The pull-through is
sealed (`registry.cache_only`) and what gets served is what an operator put here.

**The trust model is different in kind, not weaker.** Admission-time verification
answers *"did this come from the publisher it claims?"*. An operator who has
unpacked an artifact, reviewed it, run it through their own pipeline and approved
it is not asking that: they are asserting *"this exact byte sequence is the one we
approved"*, which is a **digest** and not a signature, and their process is the
authority. So no publisher signature is required, and requiring one would defeat
the feature — the likeliest upload is something they built, patched or
re-packaged, which no upstream key covers.

What stands in for the signature is **provenance**, which in an audit is the
stronger claim: a named person in their own organisation put these bytes here, on
this date, against their own approval record.

Three things follow, and each is a requirement rather than a nicety:

* **An upload can never read as a fetch.** The row carries
  `substrate.ATTEST_OPERATOR_UPLOAD` and `uploaded_by` from its first write. A
  ledger that said "fetched and verified" about an upload would be lying about
  the most important thing on it.
* **Coordinates are never checked against an upstream index.** An operator must
  be able to upload something upstream never published; validating against
  upstream would fail the feature for its most important case. They are checked
  only against the ecosystem's own naming rules, because the coordinates are a
  cache key and a storage path.
* **A replacement is deliberate and recorded.** An upload at coordinates that are
  already held is refused unless `replace` was asked for, and the displaced
  digest is written to the row. What is being overwritten may be exactly what a
  run already depends on.

**The digest is computed here, on receipt, and is that artifact's whole integrity
story**: nothing exists to re-verify an upload against. `substrate.lookup_present`
therefore checks what a redirect-based serve path can check (the object's size
against the row) and :func:`verify_object` is the full re-hash, on demand.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import CachedPackageFile
from terrapod.logging_config import get_logger
from terrapod.services import cache_admission, orphaned_objects
from terrapod.storage import keys
from terrapod.storage.protocol import ObjectNotFoundError, ObjectStore

from . import substrate
from .substrate import Coordinates

logger = get_logger(__name__)

#: The digest algorithm an upload is identified by. One algorithm, not a choice:
#: the operator is not matching anything upstream published, so there is nothing
#: for a weaker or stronger one to interoperate with, and a per-upload choice
#: would only be a way to record a digest nobody can compare.
UPLOAD_ALGORITHM = "sha256"

#: The most bytes one upload may carry.
#:
#: Deliberately generous, and NOT a policy: the pull-through path has no cap at
#: all — it streams whatever upstream serves, which for a GPU-flavoured wheel is
#: comfortably multiple gigabytes — so a cap below what a fetch accepts would
#: leave the upload path unable to supply artifacts this very cache already
#: serves, which is the one thing it exists to do. The bound is here so an
#: unbounded body cannot fill the ephemeral PVC, not to express an opinion about
#: artifact size. It is a constant rather than a Helm value because nothing has
#: asked to tune it and an inert config key is worse than none.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024

#: Read size for re-hashing a stored object during verification.
_CHUNK = 1024 * 1024


class UploadConflict(RuntimeError):
    """These coordinates are already held and `replace` was not asked for.

    Carries the existing row so the refusal can say what is there — its digest,
    how it was admitted and who by. A 409 that only says "conflict" sends an
    operator to go and look up the thing the server already had in its hand.
    """

    def __init__(self, existing: CachedPackageFile) -> None:
        self.existing = existing
        super().__init__(
            f"{existing.ecosystem} {existing.name} {existing.filename} is already "
            f"cached ({existing.digest or 'no digest recorded'}, admitted as "
            f"{existing.attestation or 'unattested'}). Pass replace=true to "
            "overwrite it; a run may already depend on what is there."
        )


@dataclass(frozen=True)
class Admission:
    """What an upload did.

    `replaced` and `unchanged` are mutually exclusive and both can be false (a
    first upload at fresh coordinates). The caller turns them into a status code
    and, when something was displaced, into the warning a sealed node owes an
    operator — the previous bytes cannot be fetched back.
    """

    record: CachedPackageFile
    replaced: bool = False
    unchanged: bool = False
    previous_digest: str = ""


@dataclass(frozen=True)
class Verification:
    """Whether a stored object still hashes to what its row records."""

    #: None when there was nothing to check against — a `none-published` row
    #: carries no digest, so "unverifiable" is a different answer from "wrong"
    #: and must not be reported as either passing or failing.
    ok: bool | None
    detail: str
    algorithm: str = ""
    recorded: str = ""
    computed: str = ""
    recorded_size: int = 0
    stored_size: int = 0


async def admit(
    db: AsyncSession,
    storage: ObjectStore,
    *,
    ecosystem: str,
    coordinates: Coordinates,
    tmp_path: str,
    uploaded_by: str,
    reference: str = "",
    replace: bool = False,
) -> Admission:
    """Admit an uploaded artifact, recording who vouched for it.

    `tmp_path` is a file on the ephemeral PVC that the caller owns and must
    unlink. The size recorded is the one measured while hashing rather than the
    caller's count of what it received, so the row describes the bytes on disk.

    **The row is flushed before the object is written.** Neither write can be
    atomic with the other, so the order is chosen for its failure mode: a failed
    put rolls the row back with the session, where the reverse would leave an old
    row describing new bytes. And no other session can see the row until the
    request commits, by which time the object is there.
    """
    size, hexdigest = await asyncio.to_thread(substrate._digest_file, tmp_path, UPLOAD_ALGORITHM)
    digest = f"{UPLOAD_ALGORITHM}:{hexdigest}"
    # Under `uploads/`, not beside the pull-through content it stands in for.
    # An uploaded artifact has no upstream, so it is irreplaceable where cached
    # content is merely cold, and `blob_classes` can only tell them apart by the
    # key -- `owning_class` resolves one class per key by longest prefix, and two
    # classes over `cache/packages/` would be counted and copied twice (#2599).
    #
    # Safe because every serve path reads `record.storage_key` rather than
    # recomputing: a row written before this keeps serving from wherever it says.
    key = keys.uploaded_package_key(ecosystem, coordinates.name, coordinates.filename)

    existing = await substrate.lookup(db, ecosystem, coordinates.name, coordinates.filename)
    if existing is not None:
        return await _admit_over(
            db,
            storage,
            existing,
            ecosystem=ecosystem,
            coordinates=coordinates,
            tmp_path=tmp_path,
            key=key,
            digest=digest,
            size=size,
            uploaded_by=uploaded_by,
            reference=reference,
            replace=replace,
        )

    record = CachedPackageFile(
        ecosystem=ecosystem,
        name=coordinates.name,
        version=coordinates.version,
        filename=coordinates.filename,
        storage_key=key,
        size=size,
        digest=digest,
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
        # first -- a conflict unless a replacement was asked for -- rather than
        # treating a race as licence to overwrite. The bytes are already hashed,
        # so this re-enters the existing-row path directly rather than calling
        # `admit` again: a 2 GB artifact must not be read twice to lose a race.
        await db.rollback()
        winner = await substrate.lookup(db, ecosystem, coordinates.name, coordinates.filename)
        if winner is None:  # pragma: no cover — the constraint says otherwise
            raise
        return await _admit_over(
            db,
            storage,
            winner,
            ecosystem=ecosystem,
            coordinates=coordinates,
            tmp_path=tmp_path,
            key=key,
            digest=digest,
            size=size,
            uploaded_by=uploaded_by,
            reference=reference,
            replace=replace,
        )

    await _put(storage, key, tmp_path, coordinates.content_type)
    logger.info(
        "Operator uploaded a cached artifact",
        ecosystem=ecosystem,
        name=coordinates.name,
        filename=coordinates.filename,
        digest=digest,
        size=size,
        by=uploaded_by,
    )
    # The ledger is the durable answer to "who vouched for this?" (#2589).
    # Best-effort by construction -- an upload must not fail because the
    # bookkeeping did.
    from terrapod.services.artifact_ledger import coordinate, record_upload

    await record_upload(
        db,
        digest=record.digest,
        kind="package",
        coordinates=coordinate(
            "package",
            ecosystem=record.ecosystem,
            name=record.name,
            version=record.version or "",
            filename=record.filename,
        ),
        uploaded_by=uploaded_by,
        storage_key=record.storage_key,
    )
    return Admission(record=record)


async def _admit_over(
    db: AsyncSession,
    storage: ObjectStore,
    existing: CachedPackageFile,
    *,
    ecosystem: str,
    coordinates: Coordinates,
    tmp_path: str,
    key: str,
    digest: str,
    size: int,
    uploaded_by: str,
    reference: str,
    replace: bool,
) -> Admission:
    """Admit an upload at coordinates something already occupies."""
    # The rule itself lives in `cache_admission` because three caches share it and
    # none of them shares a row shape. Keeping it there is what stops the binary
    # and provider caches drifting from this one (#2599).
    outcome = cache_admission.decide(
        occupied=True,
        existing_is_upload=existing.attestation == substrate.ATTEST_OPERATOR_UPLOAD,
        existing_digest=existing.digest or "",
        new_digest=digest,
        replace=replace,
    )
    if outcome is cache_admission.Outcome.UNCHANGED:
        # Re-uploading the identical artifact is not a replacement, so it is not
        # refused: the operator is re-asserting what is already recorded. The
        # object is written again because that is also the repair for one that
        # went missing or was truncated under a row that is otherwise correct --
        # which the serve path now refuses to serve, and which nothing else can
        # fix for an artifact with no upstream to re-fetch it from.
        #
        # The provenance is NOT rewritten. `uploaded_by` and `cached_at` are the
        # first assertion of these bytes, and that is the honest answer to "who
        # put this here".
        await _put(storage, key, tmp_path, coordinates.content_type)
        logger.info(
            "Operator re-uploaded an identical cached artifact",
            ecosystem=ecosystem,
            name=coordinates.name,
            filename=coordinates.filename,
            digest=digest,
            by=uploaded_by,
        )
        return Admission(record=existing, unchanged=True)
    if outcome is cache_admission.Outcome.CONFLICT:
        raise UploadConflict(existing)

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
    previous = existing.digest
    now = datetime.now(UTC)
    existing.version = coordinates.version
    existing.storage_key = key
    existing.size = size
    existing.digest = digest
    existing.attestation = substrate.ATTEST_OPERATOR_UPLOAD
    existing.uploaded_by = uploaded_by
    existing.upload_reference = reference or None
    existing.replaced_digest = previous
    existing.replaced_at = now
    existing.cached_at = now
    existing.last_accessed_at = now
    await db.flush()
    await _put(storage, key, tmp_path, coordinates.content_type)
    # WARNING rather than info: this is the one operation here that destroys
    # something, and what it destroys may be what a run already depends on.
    logger.warning(
        "Operator replaced a cached artifact",
        ecosystem=ecosystem,
        name=coordinates.name,
        filename=coordinates.filename,
        digest=digest,
        replaced_digest=previous,
        by=uploaded_by,
    )
    # The ledger is the durable answer to "who vouched for this?" (#2589).
    # Best-effort by construction -- an upload must not fail because the
    # bookkeeping did.
    from terrapod.services.artifact_ledger import coordinate, record_upload

    await record_upload(
        db,
        digest=existing.digest,
        kind="package",
        coordinates=coordinate(
            "package",
            ecosystem=existing.ecosystem,
            name=existing.name,
            version=existing.version or "",
            filename=existing.filename,
        ),
        uploaded_by=uploaded_by,
        storage_key=existing.storage_key,
    )
    return Admission(record=existing, replaced=True, previous_digest=previous)


async def _put(storage: ObjectStore, key: str, tmp_path: str, content_type: str) -> None:
    """Stream the uploaded file into object storage.

    Streamed from disk rather than buffered: an artifact here is routinely
    hundreds of megabytes and the API pod's heap is not where it goes (rule 14).
    """
    await storage.put_stream(key, substrate._file_chunks(tmp_path), content_type=content_type)


async def verify_object(storage: ObjectStore, record: CachedPackageFile) -> Verification:
    """Re-hash a stored object and compare it with what its row records.

    **This is the check a redirect-based serve path cannot do.** Every cache read
    ends in a redirect to object storage, so the API never sees the bytes a client
    is served and cannot hash them per request; what it can do on the serve path
    is compare the object's size, which `substrate.lookup_present` does for an
    uploaded row. This is the full check, on demand, and it reads the whole
    object — which for a multi-gigabyte artifact takes as long as reading a
    multi-gigabyte object takes.

    Works for a fetched row too: the recorded digest is upstream's there, and
    comparing our stored bytes against it is still the right question. A row with
    no recorded digest (`none-published`) is reported as unverifiable, which is a
    third answer and not a pass.
    """
    try:
        expected = substrate._parse_digest(record.digest)
    except substrate.DigestMismatch as exc:
        return Verification(
            ok=None,
            detail=f"the recorded digest cannot be read: {exc}",
            recorded=record.digest,
            recorded_size=record.size,
        )
    if expected is None:
        return Verification(
            ok=None,
            detail=(
                "no digest was recorded for this artifact, so there is nothing to "
                "verify it against. Its ecosystem's upstream publishes none "
                f"(attestation {record.attestation or 'unattested'})."
            ),
            recorded_size=record.size,
        )

    algorithm, recorded_hex = expected
    hasher = hashlib.new(algorithm)
    stored_size = 0
    try:
        async for chunk in storage.get_stream(record.storage_key, chunk_size=_CHUNK):
            stored_size += len(chunk)
            await asyncio.to_thread(hasher.update, chunk)
    except ObjectNotFoundError:
        return Verification(
            ok=False,
            detail=(
                f"the object is missing from storage at {record.storage_key}. "
                "An uploaded artifact cannot be re-fetched; it has to be uploaded "
                "again."
            ),
            algorithm=algorithm,
            recorded=record.digest,
            recorded_size=record.size,
        )

    computed = hasher.hexdigest()
    ok = computed == recorded_hex and stored_size == record.size
    return Verification(
        ok=ok,
        detail=(
            "the stored object matches what was recorded"
            if ok
            else "the stored object is NOT what was recorded for these coordinates"
        ),
        algorithm=algorithm,
        recorded=record.digest,
        computed=f"{algorithm}:{computed}",
        recorded_size=record.size,
        stored_size=stored_size,
    )
