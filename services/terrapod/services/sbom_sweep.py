"""Filling in the SBOM of an artifact already admitted (#2589 step 2b).

**Why this is a sweep and not part of admission.** Inventorying takes seconds,
and admission is a fetch somebody is waiting on -- a cache miss that blocks on
syft is a cache miss that feels broken. Three further properties fall out of
doing it afterwards, and each is worth more than the simplicity of doing it
inline:

- **It is retryable.** syft may be absent at admission -- not yet cached, or the
  node sealed -- and on that path the alternative is an artifact that never gets
  an inventory at all, silently.
- **It survives the fetch.** The downloaded file is unlinked from the PVC
  immediately after it reaches object storage, so an inline inventory would have
  to hold the fetch open across the scan. Afterwards, the bytes are durable and
  this reads them back.
- **It cannot affect admission.** An SBOM is evidence, never a gate -- which for
  an operator upload is the only coherent position, since gating on our own scan
  would second-guess the attestor we have just said we trust (#2599).

Registered with the distributed scheduler rather than as a bare task, so several
API replicas do not each inventory the same artifact (principle 11).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import structlog
from sqlalchemy import select

from terrapod.db.models import ArtifactLedgerEntry, now_utc

logger = structlog.get_logger(__name__)

#: How many artifacts one cycle inventories. A bound rather than "everything
#: outstanding", because the first cycle after this ships meets every artifact
#: the deployment has ever cached, and taking them a batch at a time keeps a
#: backlog from monopolising a replica for an hour.
BATCH = 20


async def sbom_cycle() -> None:
    """Inventory a batch of admitted artifacts that have no SBOM yet."""
    from terrapod.db.session import get_db_session
    from terrapod.services import sbom_service

    # Ask once, before taking a batch: with no syft there is nothing to do, and
    # streaming artifacts out of object storage to discover that per-artifact
    # would be pure waste on a sealed node.
    if await sbom_service.syft_binary() is None:
        return

    async with get_db_session() as db:
        rows = (
            (
                await db.execute(
                    select(ArtifactLedgerEntry)
                    .where(ArtifactLedgerEntry.sbom.is_(None))
                    # Oldest first, so a backlog drains in admission order rather
                    # than starving whatever happens to sort last.
                    .order_by(ArtifactLedgerEntry.admitted_at)
                    .limit(BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return

        for row in rows:
            await _apply(row)
        await db.flush()


async def _apply(row: ArtifactLedgerEntry) -> None:
    """Inventory one row and store the result, or leave it alone.

    Separate from the loop because the leave-it-alone case is the load-bearing
    one: NULL and an empty document are different facts, and writing `{}` for a
    failed inventory would make every later cycle skip the row -- turning a
    transient failure into a permanent absence that also READS as "we looked and
    found nothing", which is a reassuring claim nothing established.
    """
    document = await _inventory_one(row)
    if document is None:
        return
    row.sbom = document
    row.updated_at = now_utc()


async def _inventory_one(row: ArtifactLedgerEntry) -> dict | None:
    """Stream one artifact back out of object storage and inventory it."""
    from terrapod.services import sbom_service
    from terrapod.services.api_tool import tool_dir
    from terrapod.storage import get_storage

    key = (row.admission or {}).get("storage_key")
    if not key:
        # An admission recorded before this field existed, or one whose object
        # cannot be located. Nothing to inventory, and nothing to retry.
        return None

    storage = get_storage()
    # On the PVC, not `/tmp`: `/tmp` on the API pod is RAM-backed and a cached
    # artifact is exactly the kind of thing rule 14 keeps out of memory. Named
    # after the object's own filename so the suffix survives -- `_is_archive`
    # reads it to decide whether to extract, so a scratch name with no suffix
    # would make every archive inventory as an opaque file, which is the empty
    # SBOM this whole module exists to avoid.
    root = tool_dir() / "sbom"
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    suffix = _suffix_of(key)
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix="art.", suffix=suffix)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with tmp.open("wb") as fh:
            async for chunk in storage.get_stream(key):
                await asyncio.to_thread(fh.write, chunk)
    except Exception as exc:  # noqa: BLE001 - a missing object is not a crash
        logger.warning(
            "could not read an admitted artifact to inventory it",
            digest=row.digest,
            key=key,
            error=str(exc),
        )
        await asyncio.to_thread(tmp.unlink, True)
        return None

    try:
        return await sbom_service.inventory(tmp)
    finally:
        await asyncio.to_thread(tmp.unlink, True)


def _suffix_of(key: str) -> str:
    """The filename suffix of a storage key, for `_is_archive` to read.

    Returns `.tar.gz` whole rather than just `.gz`, because that is what the
    archive table matches on.
    """
    name = key.rsplit("/", 1)[-1].lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if name.endswith(compound):
            return compound
    _, dot, ext = name.rpartition(".")
    return f".{ext}" if dot and ext else ""
