"""Recording what executed and who vouched for it (#2589 step 2).

`artifact_ledger` answers the question an infosec team actually asks -- *show me
everything that ran, and who vouched for each of it* -- so a row is written at
ADMISSION, when the artifact is first accepted, rather than by a scanner
sweeping afterwards. An artifact that was admitted and never scanned still has a
row; it is the scan that is missing, not the record.

**Keyed on the digest**, because that is the artifact's identity. Two names for
one set of bytes is one row carrying two sets of coordinates; two different
artifacts published under one name is two rows, which is the case a
coordinate-keyed table cannot represent and is the one worth catching.

**Admission is never blocked by anything here.** A ledger failure must not fail a
fetch an operator is waiting on, and for an upload the position is stronger
still: gating on our own scan would second-guess the attestor we have just said
we trust (#2599). So every write is best-effort and says so.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import ArtifactLedgerEntry, now_utc

logger = structlog.get_logger(__name__)


def coordinate(kind: str, **parts: str) -> dict:
    """One naming of an artifact, for the `coordinates` list.

    A dict rather than a string so the parts stay queryable and a new kind can
    name itself however it needs to, instead of every reader learning to parse a
    different delimiter convention.
    """
    return {"kind": kind, **{k: v for k, v in parts.items() if v}}


async def record_admission(
    db: AsyncSession,
    *,
    digest: str,
    kind: str,
    attestation: str,
    attestor: dict[str, Any] | None = None,
    coordinates: dict | None = None,
    admission: dict[str, Any] | None = None,
) -> None:
    """Record that `digest` was admitted, or add a naming to an existing row.

    Idempotent, and deliberately NOT an overwrite. A second admission of the same
    bytes under a new name ADDS a coordinate and leaves the original
    `admitted_at`, attestation and attestor alone: the first admission is the one
    that happened, and a later pull under a different name does not re-decide who
    vouched for it.

    The one exception is an attestation that has STRENGTHENED -- a row admitted
    on a bare digest whose publisher provenance verified on a later pull. That is
    new evidence about the same bytes rather than a different answer, so it is
    taken.
    """
    # The ledger is KEYED on the digest, so an artifact that has none has no
    # identity here and must not be given one. Three ecosystems publish no
    # per-artifact digest -- Go modules, Pulumi plugins and NuGet packages all
    # pass "" -- and `""` is a perfectly valid primary key, so without this they
    # all collided on one row: the first insert won and every later admission
    # merged its coordinates into it, producing a single "artifact" with
    # thousands of coordinates, whichever `kind` arrived first, and an
    # `admitted_at` from the first fetch. `/admin/cache/artifact/{digest}`
    # cannot address it at all.
    #
    # NOT normalised here. `normalise_digest` is for the bare-hex-vs-prefixed
    # question the binary and provider caches pose; an npm digest is an SRI
    # (`sha512-<base64>`), which already names its algorithm and whose base64 is
    # CASE-SENSITIVE -- putting one through it yields `sha256:sha512-aaa...`,
    # claiming the wrong algorithm over a value it has already corrupted by
    # lowercasing. The callers normalise what needs it.
    if not (digest or "").strip():
        logger.debug(
            "not recording an admission with no digest",
            kind=kind,
            coordinates=coordinates,
        )
        return

    now = now_utc()
    coords = [coordinates] if coordinates else []

    stmt = (
        pg_insert(ArtifactLedgerEntry)
        .values(
            digest=digest,
            kind=kind,
            coordinates=coords,
            attestation=attestation,
            attestor=attestor or {},
            admitted_at=now,
            admission=admission or {},
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["digest"])
    )
    try:
        result = await db.execute(stmt)
        if result.rowcount:
            return
        # The row already exists. Merge this naming into it, in Python rather
        # than as a JSONB append, because the list is small and the comparison
        # ("is this coordinate already recorded?") is a dict equality that SQL
        # would express far less readably.
        await _merge(
            db,
            digest=digest,
            coordinates=coordinates,
            attestation=attestation,
            attestor=attestor,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - admission is never blocked by the ledger
        logger.warning(
            "could not record an admission in the artifact ledger",
            digest=digest,
            kind=kind,
            error=str(exc),
        )


async def _merge(
    db: AsyncSession,
    *,
    digest: str,
    coordinates: dict | None,
    attestation: str,
    attestor: dict[str, Any] | None,
    now: Any,
) -> None:
    row = (
        await db.execute(select(ArtifactLedgerEntry).where(ArtifactLedgerEntry.digest == digest))
    ).scalar_one_or_none()
    if row is None:  # pragma: no cover - the conflict said otherwise
        return

    changed = False
    if coordinates and coordinates not in (row.coordinates or []):
        # Reassign rather than append: SQLAlchemy does not track an in-place
        # mutation of a JSONB list, so an append would be silently discarded.
        row.coordinates = [*(row.coordinates or []), coordinates]
        changed = True

    if _is_stronger(attestation, row.attestation):
        row.attestation = attestation
        if attestor:
            row.attestor = attestor
        changed = True

    if changed:
        row.updated_at = now
        await db.flush()


#: Ordered weakest to strongest. An operator upload is not ON this scale --
#: a named person in the organisation vouching for an artifact is a different
#: KIND of claim from a cryptographic one, not a weaker or stronger one, so it
#: never upgrades or is upgraded. Recording that here rather than leaving the
#: absence to be read as an oversight.
_STRENGTH = ("none-published", "upstream-digest", "upstream-publisher")


def _is_stronger(candidate: str, existing: str) -> bool:
    if candidate not in _STRENGTH or existing not in _STRENGTH:
        return False
    return _STRENGTH.index(candidate) > _STRENGTH.index(existing)


def normalise_digest(value: str) -> str:
    """`sha256:<hex>`, whatever shape the caller holds it in.

    The ledger is KEYED on the digest, so a bare hex from one cache and a
    prefixed one from another would split identical bytes across two rows --
    defeating the one property the table is built around. The package cache
    stores `sha256:...`; the binary and provider caches store a bare `shasum`.
    """
    value = (value or "").strip()
    if not value:
        return ""
    # An SRI -- `sha512-<base64>`, which is what npm publishes -- already names
    # its algorithm, and its base64 is CASE-SENSITIVE. Lowercasing it corrupts
    # it, and prefixing `sha256:` onto a sha512 claims the wrong algorithm. It is
    # returned verbatim, so a reader joining a cache row to its ledger entry gets
    # the same key the admission wrote.
    #
    # Checked before the `:` test because an SRI has no colon at all: without
    # this, every npm digest took the prefixing branch.
    algorithm = value.split(":", 1)[0]
    if "-" in algorithm:
        return value
    value = value.lower()
    return value if ":" in value else f"sha256:{value}"


async def record_upload(
    db: AsyncSession,
    *,
    digest: str,
    kind: str,
    coordinates: dict,
    uploaded_by: str,
    storage_key: str | None = None,
) -> None:
    """Record an operator upload (#2599).

    Separate from `record_admission` only in what it asserts about the attestor:
    a named person in the organisation, rather than a publisher whose signature
    we checked. That distinction is the whole point of the single `attestation`
    column -- "who vouched for this?" has one answer per artifact, and for an
    upload the answer is a person.

    It matters more here than on the pulled path, because an upload is the one
    attestation that cannot be re-verified later: there is no publisher material
    to re-check, so this record IS the provenance.
    """
    await record_admission(
        db,
        digest=normalise_digest(digest),
        kind=kind,
        attestation="operator-upload",
        attestor={"uploaded_by": uploaded_by},
        coordinates=coordinates,
        admission={"storage_key": storage_key} if storage_key else {},
    )
