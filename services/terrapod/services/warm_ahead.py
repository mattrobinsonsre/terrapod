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

**Every ecosystem the cache holds, not the two it started with (#2486).** This
dispatched on `pypi`, `npm` and `oci` and answered "unknown ecosystem" for the
other four, while every one of them had a working pull-through adapter — so a
configure's Galaxy collections, the slowest first run there is, could not be
warmed at all. Each adapter below REUSES its proxy's own fetch rather than
re-implementing it, which is what keeps a warmed artifact byte-identical to a
pulled one and addressed by the same key.

**What a warm has to cache is not just the artifact.** Four of the six serve an
index document a sealed node cannot otherwise obtain, and warming the artifact
alone leaves a cache that looks full and cannot answer: npm's packument carries
the dependency ranges, galaxy's version list is what `ansible-galaxy` resolves
against, go's `@v/list` and nuget's `index.json` likewise. Pulumi is the one
with nothing to cache but the tarball, because its client never asks for a list.

**A warm may be sourced from somewhere other than the serving upstream.** An
operator filling a node that will be sealed often has a nearer copy — another
Terrapod, an internal mirror — and `source` names one of
`registry.package_cache.<eco>.mirrors`. By LABEL, never by URL: `upstream`'s own
documentation calls itself the whole of the request-forgery surface *because* it
is operator-set, and a warm body carrying a URL would hand an admin-authenticated
caller a server-side fetch to any host they name. Resolving a label keeps every
URL this deployment can reach inside its configuration.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client
from terrapod.services.capabilities import capability_enabled
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


#: Package ecosystems a warm can name, which is every one the cache holds.
#: Derived from nothing on purpose — `CachedPackageFile.ecosystem` documents the
#: same six and `capabilities.CAPABILITIES` carries them plus `oci`, but each of
#: those answers a different question, and a warm adapter existing is this
#: module's own fact. `test_warm_ahead.py` asserts the three agree.
PACKAGE_ECOSYSTEMS: tuple[str, ...] = ("galaxy", "pypi", "npm", "go", "nuget", "pulumi")

#: Plus the container registry, which is warmable and is not a package cache.
ECOSYSTEMS: tuple[str, ...] = (*PACKAGE_ECOSYSTEMS, "oci")

#: Ecosystems whose upstream publishes a meaningful "newest", so an unversioned
#: item is a convenience rather than a guess. Pulumi is absent because its
#: protocol has no index at all — the CLI already knows the version it wants and
#: asks for one filename — so there is nothing to resolve a bare name against.
RESOLVES_LATEST: frozenset[str] = frozenset({"galaxy", "pypi", "npm", "go", "nuget", "oci"})

#: What a Pulumi plugin warm covers when the submission names no platform. The
#: runner's own two, because a plugin is per-platform and a warm that guessed one
#: would leave the other arch to discover the gap after sealing.
DEFAULT_PULUMI_PLATFORMS: tuple[str, ...] = ("linux/amd64", "linux/arm64")


@dataclass
class WarmItem:
    """One thing to warm. `version` empty means the newest upstream offers."""

    ecosystem: str
    name: str
    version: str = ""
    #: A label in `registry.package_cache.<eco>.mirrors`, or empty for the
    #: configured upstream. Never a URL — see the module docstring.
    source: str = ""
    #: `os/arch` pairs, for the one ecosystem whose artifact is per-platform.
    #: Ignored by the other six, which have one artifact set per version.
    platforms: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}" if self.version else self.name


@dataclass
class ItemOutcome:
    """What became of one item.

    `files` is how many the item covers and `fetched` how many were pulled from
    upstream, so `fetched == 0` with `files > 0` is "already cached" — a
    distinct answer from "fetched", and the one an operator re-running a
    submission is looking for. Collapsing the two would make a resumed run
    indistinguishable from a first one (#2486).
    """

    ecosystem: str
    ref: str
    ok: bool
    detail: str = ""
    files: int = 0
    fetched: int = 0
    #: Where these bytes came from, which is not always `upstream`. Recorded per
    #: item rather than per job because a submission may name several
    #: ecosystems, and each resolves its own source.
    source: str = ""


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
    # `get_redis_client()`, NOT `get_redis()`: the latter is the FastAPI
    # dependency (`async def` + `yield`), so calling it here returns an async
    # generator and every attribute access on it raises. See the gate in
    # `tests/services/test_warm_ahead_redis.py`.
    redis = get_redis_client()
    raw = await redis.get(_KEY.format(job_id=job_id))
    if raw is None:
        return None
    payload = json.loads(raw)
    outcomes = [ItemOutcome(**o) for o in payload.pop("outcomes", [])]
    return WarmJob(**payload, outcomes=outcomes)


async def _save(job: WarmJob) -> None:
    # `get_redis_client()`, NOT `get_redis()`: the latter is the FastAPI
    # dependency (`async def` + `yield`), so calling it here returns an async
    # generator and every attribute access on it raises. See the gate in
    # `tests/services/test_warm_ahead_redis.py`.
    redis = get_redis_client()
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


def resolve_source(ecosystem: str, source: str) -> str:
    """The base URL a warm of this ecosystem pulls from, for a chosen label.

    Empty `source` is the configured upstream. A label that is not declared
    RAISES rather than falling back: silently warming from the default would
    fill the cache from the wrong place and report success, which on a node
    about to be sealed is the expensive kind of wrong.

    `oci` has no entry here because it never needed one — its upstream is chosen
    per reference already, by the host the image name starts with, against the
    operator-set `registry.oci.upstreams` map.
    """
    if ecosystem == "oci":
        if source:
            raise ValueError(
                "oci chooses its upstream from the image reference's own host, "
                "against registry.oci.upstreams — it takes no source label"
            )
        return ""
    config = getattr(settings.registry.package_cache, ecosystem)
    if not source:
        return config.upstream
    try:
        return config.mirrors[source]
    except KeyError:
        known = ", ".join(sorted(config.mirrors)) or "none"
        raise ValueError(
            f"no source named {source!r} for {ecosystem}; "
            f"declared in registry.package_cache.{ecosystem}.mirrors: {known}"
        ) from None


def effective_sources() -> dict[str, dict[str, object]]:
    """Where a warm of each ecosystem would pull from, as the UI shows it.

    The visibility half of #2486: an operator warming from the UI could not see
    which upstream a submission would reach, so "it fetched from somewhere" was
    the whole of the feedback. `upstream` is the default and `mirrors` the
    labels a submission may name instead.
    """
    cache = settings.registry.package_cache
    out: dict[str, dict[str, object]] = {}
    for ecosystem in PACKAGE_ECOSYSTEMS:
        config = getattr(cache, ecosystem)
        out[ecosystem] = {
            "enabled": capability_enabled(ecosystem),
            "upstream": config.upstream,
            "mirrors": dict(config.mirrors),
            "resolves_latest": ecosystem in RESOLVES_LATEST,
        }
    out["oci"] = {
        "enabled": capability_enabled("oci"),
        # Deliberately not an `upstream`: the reference's first path component
        # names the host, so there is no single answer to show.
        "upstream": "",
        "mirrors": dict(settings.registry.oci.upstreams),
        "resolves_latest": True,
    }
    return out


async def _ensure(db: AsyncSession, storage: ObjectStore, artifact) -> bool:
    """Cache one artifact; True when this call is what fetched it.

    The hit is checked BEFORE `get_or_fetch` rather than inferred afterwards,
    because `get_or_fetch` cannot tell its caller which half it took — and
    without the distinction a re-run of a finished submission reports the same
    thing as the first one, so an operator cannot see that it converged.
    """
    from terrapod.services.package_cache import substrate

    present = await substrate.lookup_present(
        db, storage, artifact.ecosystem, artifact.name, artifact.filename
    )
    if present is not None:
        return False
    await substrate.fetch_and_cache(db, storage, artifact)
    return True


async def _warm_one(db: AsyncSession, storage: ObjectStore, item: WarmItem) -> ItemOutcome:
    handler = _HANDLERS.get(item.ecosystem)
    if handler is None:
        known = ", ".join(ECOSYSTEMS)
        return ItemOutcome(
            ecosystem=item.ecosystem,
            ref=item.ref,
            ok=False,
            detail=f"unknown ecosystem: {item.ecosystem} (known: {known})",
        )
    try:
        base = resolve_source(item.ecosystem, item.source)
    except ValueError as exc:
        return ItemOutcome(ecosystem=item.ecosystem, ref=item.ref, ok=False, detail=str(exc))
    return await handler(db, storage, item, base)


async def _warm_pypi(
    db: AsyncSession, storage: ObjectStore, item: WarmItem, base: str = ""
) -> ItemOutcome:
    """Cache every file for a project version, plus its PEP 658 metadata sidecars.

    Every file, not the one wheel you expect to need: which wheel pip selects
    depends on the interpreter and platform doing the installing, and guessing
    wrong is exactly the discovery-after-sealing this exists to prevent.
    """
    from terrapod.services.package_cache import pypi

    index = await pypi.fetch_index(item.name, base=base or None)
    files = index.get("files", [])
    if not files:
        return ItemOutcome(
            ecosystem="pypi", ref=item.ref, ok=False, detail="no files upstream", source=base
        )

    wanted = [f for f in files if not item.version or _pypi_version(f) == item.version]
    if not wanted:
        return ItemOutcome(
            ecosystem="pypi",
            ref=item.ref,
            ok=False,
            detail=f"no files for {item.version}",
            source=base,
        )

    cached = 0
    fetched = 0
    for entry in wanted:
        fetched += await _ensure(db, storage, pypi.artifact_for(item.name, entry))
        cached += 1
        # The metadata sidecar is what lets a resolver work without downloading
        # the wheel; absent, a sealed install fetches far more than it needs.
        if entry.get("core-metadata") or entry.get("dist-info-metadata"):
            try:
                fetched += await _ensure(db, storage, pypi.metadata_artifact_for(item.name, entry))
                cached += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug("metadata sidecar not cached", name=item.name, error=str(exc))
    return ItemOutcome(
        ecosystem="pypi", ref=item.ref, ok=True, files=cached, fetched=fetched, source=base
    )


def _pypi_version(entry: dict) -> str:
    from terrapod.services.package_cache import pypi

    return pypi._version_from_filename(entry.get("filename", ""))


async def _warm_npm(
    db: AsyncSession, storage: ObjectStore, item: WarmItem, base: str = ""
) -> ItemOutcome:
    """Cache a package's tarball **and** its packument.

    The packument is not optional. A sealed node cannot serve an install without
    it — the dependency ranges live there and nowhere else — so warming a tarball
    alone produces a cache that still cannot answer `npm install`.
    """
    from terrapod.services.package_cache import npm, substrate

    packument = await npm.fetch_packument(item.name, base=base or None)
    await substrate.store_document(
        db, storage, npm.packument_artifact(item.name), json.dumps(packument).encode()
    )

    version = item.version or packument.get("dist-tags", {}).get("latest", "")
    if not version:
        return ItemOutcome(
            ecosystem="npm", ref=item.ref, ok=False, detail="no version to warm", source=base
        )
    entry = packument.get("versions", {}).get(version)
    if entry is None:
        return ItemOutcome(
            ecosystem="npm",
            ref=item.ref,
            ok=False,
            detail=f"no such version: {version}",
            source=base,
        )

    fetched = await _ensure(db, storage, npm.artifact_for(item.name, version, entry))
    return ItemOutcome(
        ecosystem="npm",
        ref=f"{item.name}@{version}",
        ok=True,
        files=2,
        # The packument is rewritten on every warm by design -- it is the one
        # document whose newest copy is the best answer a sealed node will have
        # -- so it counts as fetched whatever the tarball did.
        fetched=fetched + 1,
        source=base,
    )


async def _warm_oci(
    db: AsyncSession, storage: ObjectStore, item: WarmItem, base: str = ""
) -> ItemOutcome:
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
    return ItemOutcome(
        ecosystem="oci",
        ref=f"{item.name}:{reference}",
        ok=True,
        files=warmed + 1,
        # The mirror pull is unconditional here -- it goes through the registry's
        # own `fetch_blob` rather than the package substrate, so there is no
        # cheap hit check to make, and claiming one would be a guess.
        fetched=warmed + 1,
        source=host,
    )


async def _warm_galaxy(
    db: AsyncSession, storage: ObjectStore, item: WarmItem, base: str = ""
) -> ItemOutcome:
    """Cache a collection's tarball **and** the three documents that resolve it.

    `ansible-galaxy` does not ask for a tarball; it asks what versions exist,
    then for one version's detail, and follows the `download_url` in it. A
    sealed node holding only the artifact answers the first two with nothing and
    the install fails before it ever reaches the bytes we warmed — which is the
    whole "looks warm, cannot answer" failure this module exists to prevent.

    The collection detail is cached as well as the version list because it
    carries `highest_version`, which is what an unpinned requirement resolves
    against.
    """
    from terrapod.services.package_cache import galaxy, substrate

    namespace, _, collection = item.name.partition(".")
    if (
        not collection
        or not galaxy.valid_segment(namespace)
        or not galaxy.valid_segment(collection)
    ):
        return ItemOutcome(
            ecosystem="galaxy",
            ref=item.ref,
            ok=False,
            detail=(f"a collection is named namespace.name, each [a-z0-9_]+ — got {item.name!r}"),
            source=base,
        )

    key = galaxy.collection_key(namespace, collection)
    src = base or None

    detail = await galaxy.fetch_collection(namespace, collection, base=src)
    await substrate.store_document(
        db, storage, _galaxy_doc(key, galaxy.COLLECTION_DOC), json.dumps(detail).encode()
    )
    versions = await galaxy.fetch_versions(namespace, collection, base=src)
    await substrate.store_document(
        db, storage, _galaxy_doc(key, galaxy.VERSIONS_DOC), json.dumps(versions).encode()
    )

    version = item.version or (detail.get("highest_version") or {}).get("version") or ""
    if not version:
        return ItemOutcome(
            ecosystem="galaxy",
            ref=item.ref,
            ok=False,
            detail="upstream named no highest_version, so there is no version to warm",
            source=base,
        )
    if not galaxy.valid_version(version):
        return ItemOutcome(
            ecosystem="galaxy",
            ref=item.ref,
            ok=False,
            detail=f"bad version: {version}",
            source=base,
        )

    try:
        version_detail = await galaxy.fetch_version(namespace, collection, version, base=src)
    except substrate.NotFoundUpstream:
        return ItemOutcome(
            ecosystem="galaxy",
            ref=f"{item.name}@{version}",
            ok=False,
            detail=f"no such version: {version}",
            source=base,
        )
    await substrate.store_document(
        db,
        storage,
        _galaxy_doc(key, galaxy.version_doc(version)),
        json.dumps(version_detail).encode(),
    )

    artifact = galaxy.artifact_for(namespace, collection, version, version_detail)
    fetched = await _ensure(db, storage, artifact)
    return ItemOutcome(
        ecosystem="galaxy",
        ref=f"{item.name}@{version}",
        ok=True,
        # The tarball plus the three documents, all four of which a sealed
        # install needs.
        files=4,
        fetched=fetched + 3,
        source=base,
    )


def _galaxy_doc(key: str, filename: str):
    """A substrate artifact for one of galaxy's stored index documents.

    `store_document` writes bytes we already hold, so `upstream_url` is empty:
    nothing will ever re-fetch this from it, and a URL here would imply a fetch
    path that does not exist.
    """
    from terrapod.services.package_cache import galaxy
    from terrapod.services.package_cache.substrate import Artifact

    return Artifact(
        ecosystem=galaxy.ECOSYSTEM,
        name=key,
        version="",
        filename=filename,
        upstream_url="",
        content_type="application/json",
    )


async def _warm_go(
    db: AsyncSession, storage: ObjectStore, item: WarmItem, base: str = ""
) -> ItemOutcome:
    """Cache all three immutable files of a module version, plus `@v/list`.

    `.info`, `.mod` AND `.zip`: the toolchain reads `.mod` to resolve the
    dependency graph long before it downloads any source, so a `.zip` on its own
    is a cache a sealed `go mod download` cannot start from. `@v/list` is cached
    too, because a requirement that is not pinned to an exact version resolves
    against it.
    """
    from terrapod.services.package_cache import goproxy, substrate

    module = item.name
    if not goproxy.valid_module(module):
        return ItemOutcome(
            ecosystem="go", ref=item.ref, ok=False, detail=f"bad module path: {module}", source=base
        )

    src = base or None
    documents = 0
    try:
        listing = await goproxy.fetch_document(module, goproxy.LIST_DOC, base=src)
        await substrate.store_document(
            db, storage, goproxy.document_artifact(module, goproxy.LIST_DOC), listing
        )
        documents += 1
    except substrate.NotFoundUpstream:
        # Normal: the toolchain probes parent prefixes that are not modules, and
        # a module can legitimately publish no list. Not a reason to fail the
        # item — the version files below are what the install needs.
        logger.debug("go module has no @v/list", module=module)

    version = item.version
    if not version:
        try:
            latest = await goproxy.fetch_document(module, goproxy.LATEST_DOC, base=src)
        except substrate.NotFoundUpstream:
            return ItemOutcome(
                ecosystem="go", ref=item.ref, ok=False, detail="no such module", source=base
            )
        await substrate.store_document(
            db, storage, goproxy.document_artifact(module, goproxy.LATEST_DOC), latest
        )
        documents += 1
        version = (json.loads(latest) or {}).get("Version", "")
        if not version:
            return ItemOutcome(
                ecosystem="go",
                ref=item.ref,
                ok=False,
                detail="@latest named no Version, so there is no version to warm",
                source=base,
            )

    if not goproxy.valid_version(version):
        return ItemOutcome(
            ecosystem="go",
            ref=item.ref,
            ok=False,
            detail=f"bad version: {version} (the proxy protocol wants a leading v)",
            source=base,
        )

    fetched = 0
    for suffix in goproxy.SUFFIXES:
        fetched += await _ensure(
            db, storage, goproxy.artifact_for(module, version, suffix, base=src)
        )
    return ItemOutcome(
        ecosystem="go",
        ref=f"{module}@{version}",
        ok=True,
        files=len(goproxy.SUFFIXES) + documents,
        fetched=fetched + documents,
        source=base,
    )


async def _warm_nuget(
    db: AsyncSession, storage: ObjectStore, item: WarmItem, base: str = ""
) -> ItemOutcome:
    """Cache one `.nupkg` and the versions document `restore` resolves against.

    The service index is built per request from the caller's own base rather
    than stored, so there is nothing to warm for it — see `nuget.service_index`.
    """
    from terrapod.services.package_cache import nuget, substrate

    if not nuget.valid_id(item.name):
        return ItemOutcome(
            ecosystem="nuget",
            ref=item.ref,
            ok=False,
            detail=f"bad package id: {item.name}",
            source=base,
        )

    src = base or None
    try:
        raw = await nuget.fetch_versions(item.name, base=src)
    except substrate.NotFoundUpstream:
        return ItemOutcome(
            ecosystem="nuget", ref=item.ref, ok=False, detail="no such package", source=base
        )
    await substrate.store_document(db, storage, nuget.versions_artifact(item.name), raw)

    version = item.version
    if not version:
        published = (json.loads(raw) or {}).get("versions") or []
        # Upstream publishes this list ascending, so the newest is the last
        # entry. Taken rather than sorted: a semver sort of our own would have
        # to decide about pre-release ordering, and upstream has already decided.
        version = published[-1] if published else ""
        if not version:
            return ItemOutcome(
                ecosystem="nuget",
                ref=item.ref,
                ok=False,
                detail="upstream lists no versions, so there is none to warm",
                source=base,
            )

    if not nuget.valid_version(version):
        return ItemOutcome(
            ecosystem="nuget", ref=item.ref, ok=False, detail=f"bad version: {version}", source=base
        )

    fetched = await _ensure(db, storage, nuget.package_artifact(item.name, version, base=src))
    return ItemOutcome(
        ecosystem="nuget",
        ref=f"{nuget.normalise(item.name)}@{version}",
        ok=True,
        files=2,
        fetched=fetched + 1,
        source=base,
    )


async def _warm_pulumi(
    db: AsyncSession, storage: ObjectStore, item: WarmItem, base: str = ""
) -> ItemOutcome:
    """Cache a plugin tarball, once per platform.

    **A version is required here and optional everywhere else**, because this
    protocol has no index: the CLI already knows what it wants and asks for one
    filename, so there is no document to resolve a bare name against and a
    "latest" would be a guess dressed as a convenience.

    One artifact per `os/arch`, defaulting to the runner's own two. A plugin is
    per-platform, so warming one arch leaves the other to be discovered after
    sealing — which is the failure the whole module is here to prevent.
    """
    from terrapod.services.package_cache import pulumi_plugins

    if not item.version:
        return ItemOutcome(
            ecosystem="pulumi",
            ref=item.ref,
            ok=False,
            detail=(
                "a pulumi plugin warm needs an explicit version: the download "
                "protocol has no index, so there is nothing to resolve 'latest' against"
            ),
            source=base,
        )

    name = item.name
    kind = "resource"
    # `resource:aws` or `language:python` — the kind is part of a plugin's
    # identity and the CLI knows it, so it is accepted rather than assumed.
    if ":" in name:
        kind, _, name = name.partition(":")

    platforms = item.platforms or list(DEFAULT_PULUMI_PLATFORMS)
    files = 0
    fetched = 0
    failures: list[str] = []
    for platform in platforms:
        os_name, _, arch = platform.partition("/")
        if not os_name or not arch:
            failures.append(f"{platform}: not os/arch")
            continue
        filename = f"pulumi-{kind}-{name}-v{item.version}-{os_name}-{arch}.tar.gz"
        parts = pulumi_plugins.parse_filename(filename)
        if parts is None:
            failures.append(f"{platform}: {filename} is not a plugin filename")
            continue
        try:
            artifact = pulumi_plugins.artifact_for(filename, parts, base=base or None)
            fetched += await _ensure(db, storage, artifact)
            files += 1
        except Exception as exc:  # noqa: BLE001 — per-platform, like per-item
            failures.append(f"{platform}: {exc}"[:160])

    ref = f"{kind}:{name}@{item.version}"
    if files == 0:
        return ItemOutcome(
            ecosystem="pulumi",
            ref=ref,
            ok=False,
            detail="; ".join(failures) or "no platforms requested",
            source=base,
        )
    return ItemOutcome(
        ecosystem="pulumi",
        ref=ref,
        ok=True,
        files=files,
        fetched=fetched,
        # A partial success says which platforms are missing. Reporting it as a
        # plain success would be the half-warmed cache again, one arch at a time.
        detail="; ".join(failures),
        source=base,
    )


#: Dispatch, as a table rather than an `if` chain, so the set a warm accepts is
#: one readable thing and `test_warm_ahead.py` can derive its subject from it.
#: An ecosystem added to `PACKAGE_ECOSYSTEMS` without a handler here fails that
#: gate rather than answering "unknown ecosystem" in production.
_HANDLERS = {
    "galaxy": _warm_galaxy,
    "pypi": _warm_pypi,
    "npm": _warm_npm,
    "go": _warm_go,
    "nuget": _warm_nuget,
    "pulumi": _warm_pulumi,
    "oci": _warm_oci,
}
