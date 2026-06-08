"""Service layer for provider binary caching (network mirror protocol).

Pull-through cache for upstream provider registries. On first request for a
provider version, fetches metadata (versions + platform shasums) from upstream,
caches it in Redis, and returns mirror-protocol JSON with proxy download URLs.
Individual platform binaries are cached on-demand in object storage when a
runner actually downloads them — only the requested os/arch is fetched.

Cache layers:
  - Redis: upstream platform metadata (shasum, filename, download_url) with 24h TTL.
    Allows {version}.json to respond without hitting upstream on subsequent requests.
  - Postgres + Object Storage: actual binary files, persisted until purged.
    Created when a runner downloads a specific platform via the proxy endpoint.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.metrics import PROVIDER_CACHE_REQUESTS
from terrapod.config import settings
from terrapod.db.models import CachedProviderPackage
from terrapod.logging_config import get_logger
from terrapod.services.hashing_stream import HashingStream
from terrapod.storage.keys import provider_cache_key
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

# Redis key for cached upstream platform metadata (24h TTL)
_META_KEY_PREFIX = "tp:provider_meta"
_META_TTL = 86400  # 24 hours


def _meta_redis_key(hostname: str, namespace: str, type_: str, version: str) -> str:
    return f"{_META_KEY_PREFIX}:{hostname}:{namespace}:{type_}:{version}"


# Redis key for cached upstream version index (24h TTL)
_INDEX_KEY_PREFIX = "tp:provider_index"


def _index_redis_key(hostname: str, namespace: str, type_: str) -> str:
    return f"{_INDEX_KEY_PREFIX}:{hostname}:{namespace}:{type_}"


async def get_or_fetch_versions(
    db: AsyncSession,
    hostname: str,
    namespace: str,
    type_: str,
) -> dict:
    """Get version list from upstream (Redis-cached, 24h TTL).

    Returns the index.json-shaped dict for the network mirror protocol.
    The version index must reflect ALL upstream versions — not just the subset
    we've cached binaries for — so that terraform/tofu version constraint
    resolution works correctly.
    """
    cfg = settings.registry.provider_cache
    if hostname not in cfg.upstream_registries:
        return {"versions": {}}

    # Check Redis cache first
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    cache_key = _index_redis_key(hostname, namespace, type_)
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    # Fetch from upstream
    upstream_versions = await _fetch_upstream_versions(hostname, namespace, type_)
    if not upstream_versions:
        return {"versions": {}}

    result = {
        "versions": {v: {} for v in upstream_versions},
    }

    # Cache in Redis (24h TTL)
    await redis.set(cache_key, json.dumps(result), ex=_META_TTL)

    return result


async def get_or_fetch_platforms(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> dict:
    """Get cached platform info or fetch metadata from upstream.

    Returns the {version}.json-shaped dict for the network mirror protocol.

    Three-tier lookup:
    1. Postgres (binary cached): presigned URLs to stored binaries.
    2. Redis (metadata cached): proxy URLs with upstream shasums, no upstream call.
    3. Upstream fetch: fetches metadata JSON, caches in Redis, returns proxy URLs.

    For a mix of cached and uncached platforms, cached platforms get presigned
    URLs and uncached platforms get proxy URLs (from Redis metadata).
    """
    # The operator's eager-cache config — surfaced in every response so
    # the runner's lock extender can distinguish "deliberate skip" from
    # "compute failed" when no h1 is present for a given platform.
    configured_platforms: set[str] = {
        f"{p['os']}_{p['arch']}" for p in settings.registry.provider_cache.platforms
    }

    # --- Tier 1: check Postgres for cached binaries ---
    result = await db.execute(
        select(CachedProviderPackage).where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
            CachedProviderPackage.version == version,
        )
    )
    cached = list(result.scalars().all())

    archives: dict = {}
    cached_platforms: set[str] = set()

    if cached:
        stale_ids = []
        now = datetime.now(UTC)
        for entry in cached:
            key = provider_cache_key(hostname, namespace, type_, version, entry.filename)
            # Verify the object actually exists in storage
            if not await storage.exists(key):
                logger.warning(
                    "Stale provider cache record (object missing from storage)",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    platform=f"{entry.os}_{entry.arch}",
                )
                stale_ids.append(entry.id)
                continue

            # Touch last_accessed_at for retention tracking
            entry.last_accessed_at = now

            presigned = await storage.presigned_get_url(key)
            platform_key = f"{entry.os}_{entry.arch}"
            archive: dict = {
                "url": presigned.url,
                "hashes": [f"zh:{entry.shasum}"],
            }
            # h1 backfill: cache entries from before h1 tracking (or
            # ones whose h1 compute failed at ingest) have empty
            # h1_hash. The runner's lock-extender (and the apply-phase
            # init reusing a plan-phase lock) needs h1 — without it
            # they fall back to a full `tofu providers lock` archive
            # download, defeating the mirror. Compute h1 once from the
            # cached archive bytes, persist, and serve from then on.
            if not entry.h1_hash:
                try:
                    archive_bytes = await storage.get(key)
                    entry.h1_hash = await asyncio.to_thread(
                        _compute_h1_from_zip_bytes, archive_bytes
                    )
                    # removeprefix matches the format stored at ingest.
                    entry.h1_hash = entry.h1_hash.removeprefix("h1:")
                    logger.info(
                        "backfilled h1 for cached provider",
                        hostname=hostname,
                        provider=f"{namespace}/{type_}",
                        version=version,
                        platform=platform_key,
                    )
                except Exception:
                    # Best-effort. On failure, the runner falls back to
                    # `providers lock` for this provider — same as today.
                    logger.warning(
                        "h1 backfill failed; runner will fall back to providers lock",
                        hostname=hostname,
                        provider=f"{namespace}/{type_}",
                        version=version,
                        platform=platform_key,
                        exc_info=True,
                    )
            if entry.h1_hash:
                archive["hashes"].append(f"h1:{entry.h1_hash}")
            archives[platform_key] = archive
            cached_platforms.add(platform_key)

        # Clean up stale DB records
        if stale_ids:
            await db.execute(
                delete(CachedProviderPackage).where(CachedProviderPackage.id.in_(stale_ids))
            )
            await db.flush()

    if cached_platforms:
        PROVIDER_CACHE_REQUESTS.labels(result="hit").inc()
    else:
        PROVIDER_CACHE_REQUESTS.labels(result="miss").inc()

    def _resp() -> dict:
        return {
            "archives": archives,
            "cached_platforms": sorted(configured_platforms),
        }

    # --- Tier 2: check Redis for upstream metadata ---
    meta = await _get_cached_metadata(hostname, namespace, type_, version)

    if meta is None:
        # --- Tier 3: fetch from upstream and cache in Redis ---
        cfg = settings.registry.provider_cache
        if not cfg.warm_on_first_request:
            return _resp()

        if hostname not in cfg.upstream_registries:
            return _resp()

        meta = await _fetch_and_cache_upstream_metadata(hostname, namespace, type_, version)

    if meta is None:
        return _resp()

    # For uncached platforms: eagerly cache platforms matching the configured
    # filter (returning presigned storage URLs), and return upstream direct
    # download URLs for all others (no auth needed — public registries).

    for platform_key, platform_meta in meta.items():
        if platform_key in cached_platforms:
            continue  # Already have presigned URL from tier 1

        if platform_key in configured_platforms:
            # Eagerly cache and return presigned URL. Include h1 in the
            # response if the fetch computed one — without this the
            # runner's lock extender sees the other-arch entry as
            # zh-only and falls back to a full `tofu providers lock`
            # archive download (defeating the whole point of caching).
            os_, arch = platform_key.split("_", 1)
            try:
                url, h1 = await fetch_and_cache_single_platform(
                    db, storage, hostname, namespace, type_, version, os_, arch
                )
                hashes = [f"zh:{platform_meta['shasum']}"]
                if h1:
                    hashes.append(f"h1:{h1}")
                archives[platform_key] = {
                    "url": url,
                    "hashes": hashes,
                }
            except Exception:
                logger.warning(
                    "Failed to eagerly cache platform, falling back to upstream URL",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    platform=platform_key,
                    exc_info=True,
                )
                archives[platform_key] = {
                    "url": platform_meta["download_url"],
                    "hashes": [f"zh:{platform_meta['shasum']}"],
                }
        else:
            # Not in filter — upstream direct URL (public, no auth needed)
            archives[platform_key] = {
                "url": platform_meta["download_url"],
                "hashes": [f"zh:{platform_meta['shasum']}"],
            }

    return _resp()


def _compute_h1_from_zip_bytes(data: bytes) -> str:
    """Compute the terraform/tofu h1: dirhash from a provider zip.

    Mirrors golang.org/x/mod/sumdb/dirhash.HashZip:
      1. Sort entries by name.
      2. For each entry, write "hex(sha256(content))  name\\n".
      3. sha256 the concatenation; base64-encode the digest.
      4. Prefix with "h1:".

    Computed exactly as `tofu providers lock` would compute it itself
    given the same archive bytes. That equivalence is the whole point —
    `tofu init` at apply time recomputes h1 from its downloaded archive
    and looks for the result in the lock file. As long as ours matches
    bit-for-bit, the lock entry we inject satisfies init.
    """
    import base64
    import hashlib
    import io
    import zipfile

    h = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in sorted(zf.namelist()):
            with zf.open(name) as fh:
                content_hash = hashlib.sha256(fh.read()).hexdigest()
            h.update(f"{content_hash}  {name}\n".encode())
    return "h1:" + base64.standard_b64encode(h.digest()).decode("ascii")


async def fetch_and_cache_single_platform(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
) -> tuple[str, str]:
    """Fetch a single platform binary from upstream, cache it, return
    (presigned URL, h1 hash with `h1:` prefix-stripped — empty if compute
    failed).

    Called by the download proxy endpoint when a runner requests a specific
    platform that hasn't been cached yet.

    Tries Redis metadata first for the download URL, falls back to upstream.

    Computes the `h1:` dirhash from the just-downloaded archive bytes and
    persists it on the `CachedProviderPackage` row. The runner's lock
    extender reads h1 from the mirror response and splices it into
    .terraform.lock.hcl, avoiding the per-plan `tofu providers lock`
    archive download.
    """
    # Check Redis metadata for download info (avoids extra upstream call)
    platform_key = f"{os_}_{arch}"
    meta = await _get_cached_metadata(hostname, namespace, type_, version)
    download_url = None
    filename = None
    if meta and platform_key in meta:
        download_url = meta[platform_key].get("download_url")
        filename = meta[platform_key].get("filename")

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # If we don't have download info from Redis, fetch from upstream
        if not download_url or not filename:
            download_info = await _fetch_platform_download(
                client, hostname, namespace, type_, version, os_, arch
            )
            if download_info is None:
                raise ValueError(
                    f"Platform {os_}/{arch} not found upstream for "
                    f"{hostname}/{namespace}/{type_} v{version}"
                )
            download_url = download_info["download_url"]
            filename = download_info["filename"]

        key = provider_cache_key(hostname, namespace, type_, version, filename)

        # Two-phase: download upstream → tempfile, then upload
        # tempfile → storage AND compute h1 from the same bytes. The
        # tempfile lives on the API pod's ephemeral disk; provider
        # archives top out at ~300 MB which is well within reach.
        # Bounded memory: we iterate in 256 KB chunks at every read.
        import os
        import tempfile
        import zipfile

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
        os.close(tmp_fd)
        try:
            # Phase 1: stream upstream → tempfile (computing shasum/size
            # via HashingStream so we don't have to re-read for them).
            with open(tmp_path, "wb") as fh:
                async with client.stream("GET", download_url, timeout=300.0) as resp:
                    resp.raise_for_status()
                    stream = HashingStream(resp)
                    async for chunk in stream:
                        fh.write(chunk)
                    shasum = stream.sha256_hex
                    size_bytes = stream.size

            # Phase 2: compute h1 from the archive bytes. Wrapped in
            # try/except so a corrupted download (storage error returning
            # a non-zip body, or a download truncated before completion)
            # is logged but doesn't fail the run — we'll just persist an
            # empty h1 and the runner falls back to `tofu providers lock`
            # for that provider. Empty h1 is also how unit tests with
            # mocked streams hit this branch: no zip bytes to hash.
            h1_hash_raw = ""
            try:
                with open(tmp_path, "rb") as fh:
                    h1_hash = _compute_h1_from_zip_bytes(fh.read())
                h1_hash_raw = h1_hash.removeprefix("h1:")
            except (zipfile.BadZipFile, OSError) as exc:
                logger.warning(
                    "could not compute h1 from cached archive (h1 left empty)",
                    err=str(exc),
                    provider=f"{namespace}/{type_}",
                    version=version,
                    platform=platform_key,
                )

            # Phase 3: upload tempfile → storage.
            async def _file_chunks():
                with open(tmp_path, "rb") as fh:
                    while True:
                        buf = fh.read(256 * 1024)
                        if not buf:
                            break
                        yield buf

            await storage.put_stream(key, _file_chunks(), content_type="application/zip")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Record in database. Two API replicas can race on the same
    # cache-miss when a `tofu init` downloads N providers in parallel
    # against an empty cache — both successfully stream the binary
    # into object storage (object writes are idempotent: same content,
    # same key) but only the first INSERT wins on the
    # `uq_cached_provider_packages` unique constraint. The loser gets
    # a `UniqueViolationError`; we treat that as "lost the race, the
    # winner's row is already there" rather than letting it bubble out
    # as a 500 to the runner (which then fails `tofu init` entirely).
    # Mirror of the same handling in binary_cache_service.py.
    entry = CachedProviderPackage(
        hostname=hostname,
        namespace=namespace,
        type=type_,
        version=version,
        os=os_,
        arch=arch,
        filename=filename,
        shasum=shasum,
        h1_hash=h1_hash_raw,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        logger.info(
            "Provider cache race — another fetcher won; serving from existing row",
            hostname=hostname,
            provider=f"{namespace}/{type_}",
            version=version,
            platform=platform_key,
        )
    else:
        logger.info(
            "Provider binary cached (on-demand)",
            hostname=hostname,
            provider=f"{namespace}/{type_}",
            version=version,
            platform=platform_key,
            size_bytes=size_bytes,
        )

    presigned = await storage.presigned_get_url(key)
    return presigned.url, h1_hash_raw


async def get_cached_platform(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
) -> str | None:
    """Get a cached provider binary presigned URL, or None if not cached.

    Also cleans up stale DB records (object missing from storage).
    """
    result = await db.execute(
        select(CachedProviderPackage).where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
            CachedProviderPackage.version == version,
            CachedProviderPackage.os == os_,
            CachedProviderPackage.arch == arch,
        )
    )
    cached = result.scalars().first()
    if cached is None:
        return None

    key = provider_cache_key(hostname, namespace, type_, version, cached.filename)
    if not await storage.exists(key):
        logger.warning(
            "Stale provider cache record (object missing from storage)",
            hostname=hostname,
            provider=f"{namespace}/{type_}",
            version=version,
            platform=f"{os_}_{arch}",
        )
        await db.delete(cached)
        await db.flush()
        return None

    # Touch last_accessed_at for retention tracking
    cached.last_accessed_at = datetime.now(UTC)
    await db.flush()

    presigned = await storage.presigned_get_url(key)
    return presigned.url


# --- Admin functions ---


async def list_cached_providers(
    db: AsyncSession,
    hostname: str | None = None,
) -> list[CachedProviderPackage]:
    """List cached provider packages, optionally filtered by hostname."""
    stmt = select(CachedProviderPackage).order_by(CachedProviderPackage.cached_at.desc())
    if hostname is not None:
        stmt = stmt.where(CachedProviderPackage.hostname == hostname)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def purge_cached_provider(
    db: AsyncSession,
    storage: ObjectStore,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> int:
    """Purge all cached platforms for a provider version. Returns count deleted."""
    result = await db.execute(
        select(CachedProviderPackage).where(
            CachedProviderPackage.hostname == hostname,
            CachedProviderPackage.namespace == namespace,
            CachedProviderPackage.type == type_,
            CachedProviderPackage.version == version,
        )
    )
    entries = list(result.scalars().all())
    for entry in entries:
        key = provider_cache_key(hostname, namespace, type_, version, entry.filename)
        await storage.delete(key)
        await db.delete(entry)

    await db.flush()
    return len(entries)


# --- Redis metadata cache ---


async def _get_cached_metadata(
    hostname: str, namespace: str, type_: str, version: str
) -> dict | None:
    """Get cached upstream platform metadata from Redis.

    Returns dict mapping platform_key (e.g. "linux_amd64") to
    {"shasum": ..., "filename": ..., "download_url": ...}, or None.
    """
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        key = _meta_redis_key(hostname, namespace, type_, version)
        raw = await redis.get(key)
        if raw:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        logger.debug("Redis metadata cache miss or error", exc_info=True)
    return None


async def _cache_metadata(
    hostname: str, namespace: str, type_: str, version: str, meta: dict
) -> None:
    """Cache upstream platform metadata in Redis with TTL."""
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        key = _meta_redis_key(hostname, namespace, type_, version)
        await redis.setex(key, _META_TTL, json.dumps(meta))
    except Exception:
        logger.debug("Failed to cache metadata in Redis", exc_info=True)


# --- Internal helpers ---


async def _fetch_upstream_versions(hostname: str, namespace: str, type_: str) -> list[str]:
    """Fetch available versions from upstream registry."""
    url = f"https://{hostname}/v1/providers/{namespace}/{type_}/versions"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(
                "Upstream version fetch failed",
                hostname=hostname,
                namespace=namespace,
                type=type_,
                status=resp.status_code,
            )
            return []
        data = resp.json()

    return [v["version"] for v in data.get("versions", [])]


async def _fetch_and_cache_upstream_metadata(
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
) -> dict | None:
    """Fetch platform metadata from upstream and cache in Redis.

    Returns dict mapping platform_key to {shasum, filename, download_url},
    or None on failure.
    """
    meta: dict = {}

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        # Get the list of platforms for this version
        url = f"https://{hostname}/v1/providers/{namespace}/{type_}/versions"
        resp = await client.get(url)
        if resp.status_code != 200:
            return None

        data = resp.json()
        target_version = None
        for v in data.get("versions", []):
            if v["version"] == version:
                target_version = v
                break

        if target_version is None:
            return None

        # Fetch metadata for each platform (JSON only, no binary downloads)
        for platform in target_version.get("platforms", []):
            os_ = platform["os"]
            arch = platform["arch"]

            try:
                download_info = await _fetch_platform_download(
                    client, hostname, namespace, type_, version, os_, arch
                )
                if download_info is None:
                    continue

                platform_key = f"{os_}_{arch}"
                meta[platform_key] = {
                    "shasum": download_info["shasum"],
                    "filename": download_info["filename"],
                    "download_url": download_info["download_url"],
                }
            except Exception:
                logger.exception(
                    "Failed to fetch platform metadata",
                    hostname=hostname,
                    provider=f"{namespace}/{type_}",
                    version=version,
                    os=os_,
                    arch=arch,
                )

    if meta:
        await _cache_metadata(hostname, namespace, type_, version, meta)
        logger.info(
            "Upstream provider metadata cached in Redis",
            hostname=hostname,
            provider=f"{namespace}/{type_}",
            version=version,
            platforms=len(meta),
        )

    return meta


async def _fetch_platform_download(
    client: httpx.AsyncClient,
    hostname: str,
    namespace: str,
    type_: str,
    version: str,
    os_: str,
    arch: str,
) -> dict | None:
    """Fetch download info for a specific platform from upstream."""
    url = f"https://{hostname}/v1/providers/{namespace}/{type_}/{version}/download/{os_}/{arch}"
    resp = await client.get(url)
    if resp.status_code != 200:
        return None
    return resp.json()
