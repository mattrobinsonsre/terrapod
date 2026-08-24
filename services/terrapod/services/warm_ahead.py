"""Warming the package and container caches ahead of a seal (#1420).

Preparing an air-gapped deployment was a rehearsal: you warmed the caches by
running the thing you expected to need — a `pip install`, a `docker pull` — and
hoped you had guessed the set right. The failure mode is discovering the gap
*after* sealing, which is the expensive moment to find it.

Three properties this has to have, and each shapes the code:

**Per-item outcomes.** Warming twenty packages and being told "failed" is not
actionable; the whole point is to find the gaps before sealing, which means
knowing precisely which ones.

**Idempotent and resumable.** A large set will hit a transient upstream failure.
Re-running must skip what is already cached and retry only the rest — which comes
free from `get_or_fetch`, and is why warming goes through it rather than fetching
directly.

**Not request-scoped.** A few hundred packages outlives an HTTP request, so a
submission returns a job id and the work happens on the scheduler's queue. A
synchronous call that times out halfway leaves the operator unsure what landed,
which is the state warming exists to get them out of.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: Warm jobs are operational actions, not records — an operator submits one,
#: watches it, and acts. Redis with a day's TTL rather than a table, so a
#: transient progress report does not become schema to migrate.
_KEY = "tp:warm:{job_id}"
_TTL_SECONDS = 86400

#: A guard against a paste that was meant for a different box. Warming is bounded
#: by upstream's patience rather than ours, and thousands of items in one
#: submission is a mistake worth catching at the door.
MAX_ITEMS = 500


@dataclass
class WarmItem:
    """One thing to warm. `version` empty means the newest upstream offers."""

    ecosystem: str  # "pypi" | "npm" | "oci"
    name: str
    version: str = ""

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}" if self.version else self.name


@dataclass
class ItemOutcome:
    ecosystem: str
    ref: str
    ok: bool
    detail: str = ""
    files: int = 0


@dataclass
class WarmJob:
    job_id: str
    status: str  # "queued" | "running" | "finished"
    total: int
    submitted_at: str
    completed: int = 0
    outcomes: list[ItemOutcome] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.ok)


async def create_job(items: list[WarmItem]) -> WarmJob:
    """Record a submission and return it, before any work is attempted."""
    job = WarmJob(
        job_id=f"warm-{uuid.uuid4().hex[:12]}",
        status="queued",
        total=len(items),
        submitted_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    await _save(job)
    return job


async def get_job(job_id: str) -> WarmJob | None:
    redis = get_redis()
    raw = await redis.get(_KEY.format(job_id=job_id))
    if raw is None:
        return None
    payload = json.loads(raw)
    outcomes = [ItemOutcome(**o) for o in payload.pop("outcomes", [])]
    return WarmJob(**payload, outcomes=outcomes)


async def _save(job: WarmJob) -> None:
    redis = get_redis()
    payload = {
        "job_id": job.job_id,
        "status": job.status,
        "total": job.total,
        "submitted_at": job.submitted_at,
        "completed": job.completed,
        "outcomes": [asdict(o) for o in job.outcomes],
    }
    await redis.set(_KEY.format(job_id=job.job_id), json.dumps(payload), ex=_TTL_SECONDS)


async def run_job(job_id: str, items: list[WarmItem]) -> None:
    """Warm every item, recording each outcome as it lands.

    Progress is saved per item rather than at the end, so an operator polling a
    long run sees it advancing — and so a run that dies partway still says how
    far it got, which is the difference between resuming and starting over.

    One item failing never stops the rest: the report is the deliverable, and a
    run that aborts on the first 404 tells the operator about one gap when they
    asked about all of them.
    """
    from terrapod.db.session import get_db_session
    from terrapod.storage import get_storage

    job = await get_job(job_id)
    if job is None:
        logger.warning("Warm job vanished before it ran", job_id=job_id)
        return
    job.status = "running"
    await _save(job)

    storage = get_storage()
    for item in items:
        try:
            async with get_db_session() as db:
                outcome = await _warm_one(db, storage, item)
        except Exception as exc:  # noqa: BLE001 — the report is the deliverable
            outcome = ItemOutcome(
                ecosystem=item.ecosystem, ref=item.ref, ok=False, detail=str(exc)[:300]
            )
        job.outcomes.append(outcome)
        job.completed += 1
        await _save(job)

    job.status = "finished"
    await _save(job)
    logger.info(
        "Warm job finished",
        job_id=job_id,
        total=job.total,
        succeeded=job.succeeded,
        failed=job.failed,
    )


async def _warm_one(db: AsyncSession, storage: ObjectStore, item: WarmItem) -> ItemOutcome:
    if item.ecosystem == "pypi":
        return await _warm_pypi(db, storage, item)
    if item.ecosystem == "npm":
        return await _warm_npm(db, storage, item)
    if item.ecosystem == "oci":
        return await _warm_oci(db, storage, item)
    return ItemOutcome(ecosystem=item.ecosystem, ref=item.ref, ok=False, detail="unknown ecosystem")


async def _warm_pypi(db: AsyncSession, storage: ObjectStore, item: WarmItem) -> ItemOutcome:
    """Cache every file for a project version, plus its PEP 658 metadata sidecars.

    Every file, not the one wheel you expect to need: which wheel pip selects
    depends on the interpreter and platform doing the installing, and guessing
    wrong is exactly the discovery-after-sealing this exists to prevent.
    """
    from terrapod.services.package_cache import pypi, substrate

    index = await pypi.fetch_index(item.name)
    files = index.get("files", [])
    if not files:
        return ItemOutcome(ecosystem="pypi", ref=item.ref, ok=False, detail="no files upstream")

    wanted = [f for f in files if not item.version or _pypi_version(f) == item.version]
    if not wanted:
        return ItemOutcome(
            ecosystem="pypi", ref=item.ref, ok=False, detail=f"no files for {item.version}"
        )

    cached = 0
    for entry in wanted:
        await substrate.get_or_fetch(db, storage, pypi.artifact_for(item.name, entry))
        cached += 1
        # The metadata sidecar is what lets a resolver work without downloading
        # the wheel; absent, a sealed install fetches far more than it needs.
        if entry.get("core-metadata") or entry.get("dist-info-metadata"):
            try:
                await substrate.get_or_fetch(
                    db, storage, pypi.metadata_artifact_for(item.name, entry)
                )
                cached += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("metadata sidecar not cached", name=item.name, error=str(exc))
    return ItemOutcome(ecosystem="pypi", ref=item.ref, ok=True, files=cached)


def _pypi_version(entry: dict) -> str:
    from terrapod.services.package_cache import pypi

    return pypi._version_from_filename(entry.get("filename", ""))


async def _warm_npm(db: AsyncSession, storage: ObjectStore, item: WarmItem) -> ItemOutcome:
    """Cache a package's tarball **and** its packument.

    The packument is not optional. A sealed node cannot serve an install without
    it — the dependency ranges live there and nowhere else — so warming a tarball
    alone produces a cache that still cannot answer `npm install`.
    """
    from terrapod.services.package_cache import npm, substrate

    packument = await npm.fetch_packument(item.name)
    await substrate.store_document(
        db, storage, npm.packument_artifact(item.name), json.dumps(packument).encode()
    )

    version = item.version or packument.get("dist-tags", {}).get("latest", "")
    if not version:
        return ItemOutcome(ecosystem="npm", ref=item.ref, ok=False, detail="no version to warm")
    entry = packument.get("versions", {}).get(version)
    if entry is None:
        return ItemOutcome(
            ecosystem="npm", ref=item.ref, ok=False, detail=f"no such version: {version}"
        )

    await substrate.get_or_fetch(db, storage, npm.artifact_for(item.name, version, entry))
    return ItemOutcome(ecosystem="npm", ref=f"{item.name}@{version}", ok=True, files=2)


async def _warm_oci(db: AsyncSession, storage: ObjectStore, item: WarmItem) -> ItemOutcome:
    """Pull an image through the mirror, manifest and every blob it references.

    Manifest-only would be worse than useless: it would look cached and fail on
    the first layer a sealed node tried to serve.
    """
    from terrapod.services.oci import pullthrough_service, registry_service, upload_service
    from terrapod.services.oci.names import parse_digest
    from terrapod.storage import keys as storage_keys

    reference = item.version or "latest"
    resolved = pullthrough_service.resolve_upstream(item.name)
    if resolved is None or not pullthrough_service.mirroring_allowed():
        return ItemOutcome(
            ecosystem="oci",
            ref=item.ref,
            ok=False,
            detail=(
                "not a mirrorable repository — its first path component must name a "
                "host in registry.oci.upstreams"
            ),
        )
    host, upstream_repo = resolved
    repository = await pullthrough_service.ensure_mirror_repository(db, item.name, host)
    repository.labels = {"access": "everyone"}

    body, media_type, digest = await pullthrough_service.fetch_manifest(
        host, upstream_repo, reference
    )
    manifest = await registry_service.store_manifest(
        db, storage, repository, digest, media_type, body
    )
    await registry_service.set_tag(db, repository, reference, manifest, from_upstream=True)

    document = json.loads(body)
    descriptors = [document.get("config") or {}, *document.get("layers", [])]
    warmed = 0
    for descriptor in descriptors:
        raw = descriptor.get("digest")
        if not raw:
            continue
        parsed = parse_digest(raw)
        storage_key = storage_keys.oci_blob_key(parsed.storage_segment)
        size = await pullthrough_service.fetch_blob(
            host, upstream_repo, parsed, storage, storage_key
        )
        blob = await upload_service._upsert_blob(db, str(parsed), size, storage_key)
        await upload_service.link_blob(db, repository, blob)
        warmed += 1

    await db.commit()
    return ItemOutcome(ecosystem="oci", ref=f"{item.name}:{reference}", ok=True, files=warmed + 1)
