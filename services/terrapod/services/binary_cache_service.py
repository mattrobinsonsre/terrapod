"""Service layer for terraform/tofu CLI binary caching.

Pull-through cache: on first request, downloads the binary from upstream
(releases.hashicorp.com for terraform, GitHub releases for tofu),
stores it in object storage, and returns a presigned download URL.
Subsequent requests serve from cache.
"""

from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.metrics import BINARY_CACHE_REQUESTS
from terrapod.config import settings
from terrapod.db.models import CachedBinary
from terrapod.http_retry import arequest_with_retry
from terrapod.logging_config import get_logger
from terrapod.services.artifact_verification import VerificationError, verify_binary
from terrapod.services.hashing_stream import HashingStream
from terrapod.storage.keys import (
    binary_cache_key,
    binary_cache_sums_key,
    binary_cache_sums_sig_key,
)
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

VALID_TOOLS = {"terraform", "tofu", "terragrunt"}
VALID_OS = {"linux", "darwin", "windows", "freebsd", "openbsd", "solaris"}
VALID_ARCH = {"amd64", "arm64", "arm", "386"}

# Redis key prefix and TTL for version resolution cache
_VERSION_CACHE_PREFIX = "tp:version_resolve"
_VERSION_CACHE_TTL = 3600  # 1 hour

# Pre-release stability tiers, least → most stable.
# Both terraform and tofu use these suffixes (tofu does not emit "dev").
_PRERELEASE_TAGS = ("dev", "alpha", "beta", "rc")
_STABILITY_RANK = {"dev": 1, "alpha": 2, "beta": 3, "rc": 4, "stable": 5}
# Floor imposed by the allow_prerelease policy value — the lowest rank accepted.
_POLICY_FLOOR = {
    "none": _STABILITY_RANK["stable"],
    "rc": _STABILITY_RANK["rc"],
    "beta": _STABILITY_RANK["beta"],
    "alpha": _STABILITY_RANK["alpha"],
    "dev": _STABILITY_RANK["dev"],
}


def _parse_stability(version: str) -> str:
    """Return the stability tier of a version string.

    Returns 'stable' for GA versions (e.g. '1.15.0'), or the matching
    pre-release tier name (e.g. 'rc' for '1.15.0-rc2').
    """
    for tag in _PRERELEASE_TAGS:
        if f"-{tag}" in version:
            return tag
    return "stable"


def _is_version_allowed(version: str, policy: str) -> bool:
    """True if `version` satisfies the pre-release `policy`."""
    return _STABILITY_RANK[_parse_stability(version)] >= _POLICY_FLOOR.get(
        policy, _STABILITY_RANK["stable"]
    )


def _version_sort_key(v: str) -> tuple:
    """Sort key giving correct ordering across stable + pre-release versions.

    Orders: 1.15.0 > 1.15.0-rc2 > 1.15.0-rc1 > 1.15.0-beta1 > 1.15.0-alpha2.
    Base x.y.z components compare first; within the same base, stability rank
    then intra-tier number decide.
    """
    base = v
    tier_rank = _STABILITY_RANK["stable"]
    tier_num = 0
    for tag in _PRERELEASE_TAGS:
        marker = f"-{tag}"
        idx = v.find(marker)
        if idx != -1:
            base = v[:idx]
            suffix = v[idx + len(marker) :]
            tier_rank = _STABILITY_RANK[tag]
            try:
                tier_num = int(suffix) if suffix else 0
            except ValueError:
                tier_num = 0
            break
    try:
        base_parts = tuple(int(x) for x in base.split("."))
    except ValueError:
        base_parts = (0,)
    return base_parts + (tier_rank, tier_num)


async def get_or_cache_binary(
    db: AsyncSession,
    storage: ObjectStore,
    tool: str,
    version: str,
    os_: str,
    arch: str,
) -> str:
    """Get a cached binary or fetch from upstream on cache miss.

    Returns a presigned download URL.
    """
    if tool not in VALID_TOOLS:
        raise ValueError(f"Invalid tool: {tool}. Must be one of {VALID_TOOLS}")
    if os_ not in VALID_OS:
        raise ValueError(f"Invalid OS: {os_}. Must be one of {VALID_OS}")
    if arch not in VALID_ARCH:
        raise ValueError(f"Invalid arch: {arch}. Must be one of {VALID_ARCH}")

    policy = settings.registry.binary_cache.allow_prerelease
    if not _is_version_allowed(version, policy):
        raise ValueError(
            f"Pre-release version {version!r} is not allowed by the current "
            f"binary_cache.allow_prerelease policy ({policy!r}). Set the "
            f"policy to 'rc', 'beta', 'alpha', or 'dev' to permit it."
        )

    # Check cache
    cached = await _get_cached(db, tool, version, os_, arch)
    if cached is not None:
        BINARY_CACHE_REQUESTS.labels(tool=tool, result="hit").inc()
        # Touch last_accessed_at for retention tracking
        cached.last_accessed_at = datetime.now(UTC)
        await db.flush()
        key = binary_cache_key(tool, version, os_, arch)
        presigned = await storage.presigned_get_url(key)
        return presigned.url

    # Cache miss — fetch from upstream
    BINARY_CACHE_REQUESTS.labels(tool=tool, result="miss").inc()
    logger.info(
        "Binary cache miss, fetching from upstream",
        tool=tool,
        version=version,
        os=os_,
        arch=arch,
    )

    if tool == "terraform":
        download_url = _terraform_download_url(version, os_, arch)
    elif tool == "terragrunt":
        download_url = _terragrunt_download_url(version, os_, arch)
    else:
        download_url = _tofu_download_url(version, os_, arch)

    # Stream directly to object storage. terraform/tofu ship a zip; terragrunt
    # ships a bare per-platform binary, so the stored content type differs.
    key = binary_cache_key(tool, version, os_, arch)
    content_type = "application/octet-stream" if tool == "terragrunt" else "application/zip"
    shasum, size_bytes = await _fetch_and_store_binary(
        storage, key, download_url, content_type=content_type
    )

    # Integrity gate (#607): verify the downloaded binary against the publisher's
    # signed SHA256SUMS before recording it. The DB row is what gates serving
    # (no row → cache miss → never returned), so verifying before the INSERT
    # below means a tampered binary is never served. On failure we also delete
    # the just-written object so it doesn't linger orphaned in storage. The
    # binary is *executed* on every run, so this is fail-closed by default.
    verify_level = settings.registry.binary_cache.verify
    if verify_level != "off":
        try:
            async with httpx.AsyncClient(follow_redirects=True) as vclient:
                manifest, sig = await verify_binary(
                    vclient, tool, version, os_, arch, shasum, level=verify_level
                )
        except VerificationError:
            await storage.delete(key)
            BINARY_CACHE_REQUESTS.labels(tool=tool, result="verify_failed").inc()
            raise
        # Persist the signed manifest + sig so the runner can independently
        # re-verify the executable against the publisher's signature with its
        # own pinned key — no upstream reach needed (#607). Only when we have
        # both (signature mode); checksum mode has no sig to serve.
        if sig is not None:
            await storage.put(
                binary_cache_sums_key(tool, version), manifest, content_type="text/plain"
            )
            await storage.put(
                binary_cache_sums_sig_key(tool, version),
                sig,
                content_type="application/pgp-signature",
            )

    # Record in database. Two concurrent cache misses for the same
    # (tool, version, os, arch) — typical when two runners spin up
    # within milliseconds of each other against an empty cache — both
    # successfully stream the binary into object storage (object writes
    # are idempotent: same content, same key) but only the first INSERT
    # wins on the `uq_cached_binaries` unique constraint. The loser
    # gets a `UniqueViolationError`; we treat that as "lost the race,
    # serve from the row the winner just inserted" rather than letting
    # it bubble out as a 5xx to the runner.
    entry = CachedBinary(
        tool=tool,
        version=version,
        os=os_,
        arch=arch,
        shasum=shasum,
        download_url=download_url,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        logger.info(
            "Binary cache race — another fetcher won; serving from existing row",
            tool=tool,
            version=version,
            os=os_,
            arch=arch,
        )
    else:
        logger.info(
            "Binary cached",
            tool=tool,
            version=version,
            os=os_,
            arch=arch,
            size_bytes=size_bytes,
        )

    presigned = await storage.presigned_get_url(key)
    return presigned.url


async def get_or_cache_sums(storage: ObjectStore, tool: str, version: str) -> tuple[bytes, bytes]:
    """Return the (SHA256SUMS, detached-sig) bytes for a tool/version (#607).

    Serves the runner's cache-path executable verification: the runner fetches
    these and re-verifies the downloaded binary against the publisher signature
    with its own pinned key. Lazily fetches from upstream + verifies + persists
    if not already cached (e.g. a binary cached before this feature shipped).
    Raises on unverifiable/unavailable material — fail closed.
    """
    if tool not in VALID_TOOLS:
        raise ValueError(f"Invalid tool: {tool}. Must be one of {VALID_TOOLS}")

    sums_key = binary_cache_sums_key(tool, version)
    sig_key = binary_cache_sums_sig_key(tool, version)
    if await storage.exists(sums_key) and await storage.exists(sig_key):
        return await storage.get(sums_key), await storage.get(sig_key)

    # Not persisted yet — fetch from upstream, verify the signature against the
    # pinned publisher key, persist, and return. Import locally to avoid a
    # module-load cycle (artifact_verification imports config, not this module).
    from terrapod.services.artifact_verification import fetch_sums_and_sig

    async with httpx.AsyncClient(follow_redirects=True) as client:
        manifest, sig = await fetch_sums_and_sig(client, tool, version, level="signature")
    if sig is None:  # pragma: no cover - signature level always returns a sig
        raise ValueError("no signature available for SHA256SUMS")
    await storage.put(sums_key, manifest, content_type="text/plain")
    await storage.put(sig_key, sig, content_type="application/pgp-signature")
    return manifest, sig


async def list_cached_binaries(
    db: AsyncSession,
    tool: str | None = None,
) -> list[CachedBinary]:
    """List cached binaries, optionally filtered by tool."""
    stmt = select(CachedBinary).order_by(CachedBinary.cached_at.desc())
    if tool is not None:
        stmt = stmt.where(CachedBinary.tool == tool)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def purge_binary(
    db: AsyncSession,
    storage: ObjectStore,
    tool: str,
    version: str,
) -> int:
    """Purge all cached binaries for a tool+version. Returns count deleted."""
    result = await db.execute(
        select(CachedBinary).where(
            CachedBinary.tool == tool,
            CachedBinary.version == version,
        )
    )
    entries = list(result.scalars().all())
    for entry in entries:
        key = binary_cache_key(tool, version, entry.os, entry.arch)
        await storage.delete(key)
        await db.delete(entry)

    await db.flush()
    return len(entries)


async def warm_binary(
    db: AsyncSession,
    storage: ObjectStore,
    tool: str,
    version: str,
    os_: str,
    arch: str,
) -> str:
    """Pre-warm a binary into the cache. Returns presigned URL."""
    return await get_or_cache_binary(db, storage, tool, version, os_, arch)


# --- Available Versions ---


async def list_available_versions(tool: str) -> list[str]:
    """List available stable versions for a tool, newest first.

    Fetches from upstream and caches in Redis for 1 hour.
    Returns both exact versions and major.minor shortcuts.
    """
    if tool not in VALID_TOOLS:
        raise ValueError(f"Invalid tool: {tool}. Must be one of {VALID_TOOLS}")

    # Check Redis cache
    cache_key = f"tp:versions:{tool}"
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        cached = await redis.get(cache_key)
        if cached:
            import json

            raw = cached.decode() if isinstance(cached, bytes) else cached
            return json.loads(raw)
    except Exception:
        pass

    if tool == "terraform":
        versions = await _fetch_terraform_versions()
    elif tool == "terragrunt":
        versions = await _fetch_terragrunt_versions()
    else:
        versions = await _fetch_tofu_versions()

    # Sort descending (stability-aware so pre-release versions sort correctly)
    versions.sort(key=_version_sort_key, reverse=True)

    # Add major.minor shortcuts (deduplicated, in order)
    shortcuts: list[str] = []
    seen: set[str] = set()
    for v in versions:
        parts = v.split(".")
        if len(parts) >= 2:
            shortcut = f"{parts[0]}.{parts[1]}"
            if shortcut not in seen:
                seen.add(shortcut)
                shortcuts.append(shortcut)

    result = shortcuts + versions

    # Cache in Redis
    try:
        import json

        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        await redis.setex(cache_key, 3600, json.dumps(result))
    except Exception:
        pass

    return result


async def _fetch_terraform_versions() -> list[str]:
    """Fetch terraform versions from releases.hashicorp.com.

    Filters by the configured allow_prerelease policy: stable-only by default,
    or includes rc/beta/alpha/dev tiers down to the configured floor.
    """
    url = "https://releases.hashicorp.com/terraform/index.json"
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Upstream GET — idempotent by method; retried on flaky-upstream
        # transient failures (connection errors, read-timeouts, 5xx).
        resp = await arequest_with_retry(client, "GET", url)
        resp.raise_for_status()
        data = resp.json()

    policy = settings.registry.binary_cache.allow_prerelease
    versions = []
    for v in data.get("versions", {}):
        if not _is_version_allowed(v, policy):
            continue
        parts = v.split("-")[0].split(".")
        if len(parts) >= 3:
            versions.append(v)
    return versions


# Official OpenTofu version index — the same one OpenTofu's own installer uses.
# We resolve tofu versions from here instead of the GitHub releases API, which is
# rate-limited (60 req/hr unauthenticated) and routinely 504s from CI/cloud IPs.
# When that listing failed, the binary cache could not resolve a partial version
# (e.g. "1.12") and 502'd the runner, which then dead-ended because it had no
# fully-qualified version to fall back on (#338). This CDN-backed static JSON is
# not rate-limited. Version ids carry NO leading "v" and include pre-releases as
# "x.y.z-suffix", so the allow_prerelease policy still applies on the string.
_TOFU_VERSION_INDEX_URL = "https://get.opentofu.org/tofu/api.json"


async def _fetch_tofu_version_ids() -> list[str]:
    """Return every OpenTofu release version id from the official index."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await arequest_with_retry(client, "GET", _TOFU_VERSION_INDEX_URL)
        resp.raise_for_status()
        data = resp.json()
    return [v["id"] for v in data.get("versions", []) if v.get("id")]


async def _fetch_tofu_versions() -> list[str]:
    """Fetch the list of installable tofu versions (allow_prerelease applied)."""
    policy = settings.registry.binary_cache.allow_prerelease
    versions = []
    for version in await _fetch_tofu_version_ids():
        if not _is_version_allowed(version, policy):
            continue
        parts = version.split("-")[0].split(".")
        if len(parts) >= 3:
            versions.append(version)
    return versions


async def _fetch_terragrunt_versions() -> list[str]:
    """Fetch terragrunt versions from GitHub releases (gruntwork-io/terragrunt).

    Same shape as `_fetch_tofu_versions`: GitHub's `prerelease` flag plus the
    configured allow_prerelease policy gate which versions are offered.
    """
    url = "https://api.github.com/repos/gruntwork-io/terragrunt/releases"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await arequest_with_retry(client, "GET", url, params={"per_page": 100})
        resp.raise_for_status()
        releases = resp.json()

    policy = settings.registry.binary_cache.allow_prerelease
    versions = []
    for release in releases:
        tag = release.get("tag_name", "")
        version = tag.lstrip("v")
        if not _is_version_allowed(version, policy):
            continue
        parts = version.split("-")[0].split(".")
        if len(parts) >= 3:
            versions.append(version)
    return versions


# --- Version Resolution ---


async def resolve_version(tool: str, partial_version: str) -> str:
    """Resolve a partial version (e.g. '1.9') to the latest exact version (e.g. '1.9.8').

    Handles:
    - Empty/None/"latest" → latest stable version
    - Two-component (x.y) → latest x.y.z patch
    - Three-component (x.y.z) → returned as-is

    Results are cached in Redis for 1 hour.
    """
    # Normalize empty, None, or "latest" to the latest stable release
    if not partial_version or partial_version.strip().lower() == "latest":
        versions = await list_available_versions(tool)
        # list_available_versions returns shortcuts first, then full versions
        # Find the first full x.y.z version
        for v in versions:
            if len(v.split(".")) >= 3:
                return v
        raise ValueError(f"No stable versions found for {tool}")

    parts = partial_version.split(".")
    if len(parts) >= 3:
        return partial_version  # Already exact

    # Check Redis cache
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        cache_key = f"{_VERSION_CACHE_PREFIX}:{tool}:{partial_version}"
        cached = await redis.get(cache_key)
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        pass  # Redis unavailable — resolve without cache

    if tool == "terraform":
        resolved = await _resolve_terraform_version(partial_version)
    elif tool == "tofu":
        resolved = await _resolve_tofu_version(partial_version)
    elif tool == "terragrunt":
        resolved = await _resolve_terragrunt_version(partial_version)
    else:
        return partial_version

    # Cache the result
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        await redis.setex(cache_key, _VERSION_CACHE_TTL, resolved)
    except Exception:
        pass

    logger.info(
        "Resolved partial version",
        tool=tool,
        partial=partial_version,
        resolved=resolved,
    )
    return resolved


async def _resolve_terraform_version(partial: str) -> str:
    """Resolve partial terraform version via releases.hashicorp.com index.

    Honors the allow_prerelease policy: pre-release versions are only
    considered when explicitly permitted.
    """
    url = "https://releases.hashicorp.com/terraform/index.json"
    prefix = f"{partial}."
    policy = settings.registry.binary_cache.allow_prerelease

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await arequest_with_retry(client, "GET", url)
        resp.raise_for_status()
        data = resp.json()

    versions = data.get("versions", {})
    matching = []
    for v in versions:
        if not v.startswith(prefix):
            continue
        if not _is_version_allowed(v, policy):
            continue
        matching.append(v)

    if not matching:
        logger.warning("No matching terraform version found", partial=partial, policy=policy)
        return partial

    matching.sort(key=_version_sort_key)
    return matching[-1]


async def _resolve_tofu_version(partial: str) -> str:
    """Resolve a partial tofu version (e.g. "1.12") to the highest matching
    exact release via the official OpenTofu version index (#338).

    Honors the allow_prerelease policy: pre-release versions are only
    considered when explicitly permitted.
    """
    prefix = f"{partial}."  # index ids carry no leading "v"
    policy = settings.registry.binary_cache.allow_prerelease

    matching = []
    for version in await _fetch_tofu_version_ids():
        if not version.startswith(prefix):
            continue
        if not _is_version_allowed(version, policy):
            continue
        matching.append(version)

    if not matching:
        logger.warning("No matching tofu version found", partial=partial, policy=policy)
        return partial

    matching.sort(key=_version_sort_key)
    return matching[-1]


async def _resolve_terragrunt_version(partial: str) -> str:
    """Resolve partial terragrunt version via GitHub releases (gruntwork-io/terragrunt).

    Mirrors `_resolve_tofu_version`; honors the allow_prerelease policy.
    """
    url = "https://api.github.com/repos/gruntwork-io/terragrunt/releases"
    prefix = f"v{partial}."
    policy = settings.registry.binary_cache.allow_prerelease

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await arequest_with_retry(client, "GET", url, params={"per_page": 100})
        resp.raise_for_status()
        releases = resp.json()

    matching = []
    for release in releases:
        tag = release.get("tag_name", "")
        if not tag.startswith(prefix):
            continue
        version = tag.lstrip("v")
        if not _is_version_allowed(version, policy):
            continue
        matching.append(version)

    if not matching:
        logger.warning("No matching terragrunt version found", partial=partial, policy=policy)
        return partial

    matching.sort(key=_version_sort_key)
    return matching[-1]


# --- Internal helpers ---


async def _get_cached(
    db: AsyncSession,
    tool: str,
    version: str,
    os_: str,
    arch: str,
) -> CachedBinary | None:
    result = await db.execute(
        select(CachedBinary).where(
            CachedBinary.tool == tool,
            CachedBinary.version == version,
            CachedBinary.os == os_,
            CachedBinary.arch == arch,
        )
    )
    return result.scalars().first()


def _terraform_download_url(version: str, os_: str, arch: str) -> str:
    """Build the upstream download URL for a terraform binary."""
    cfg = settings.registry.binary_cache
    filename = f"terraform_{version}_{os_}_{arch}.zip"
    return f"{cfg.terraform_mirror_url}/{version}/{filename}"


def _tofu_download_url(version: str, os_: str, arch: str) -> str:
    """Build the upstream download URL for a tofu binary."""
    cfg = settings.registry.binary_cache
    filename = f"tofu_{version}_{os_}_{arch}.zip"
    return f"{cfg.tofu_mirror_url}/v{version}/{filename}"


def _terragrunt_download_url(version: str, os_: str, arch: str) -> str:
    """Build the upstream download URL for a terragrunt binary.

    Terragrunt releases a bare per-platform binary (not a zip/tarball):
    `terragrunt_<os>_<arch>` under the GitHub release `v<version>` tag. The
    os/arch tokens match Terrapod's (linux/darwin/windows × amd64/arm64).
    """
    cfg = settings.registry.binary_cache
    filename = f"terragrunt_{os_}_{arch}"
    return f"{cfg.terragrunt_mirror_url}/v{version}/{filename}"


async def _fetch_and_store_binary(
    storage: ObjectStore,
    key: str,
    url: str,
    content_type: str = "application/zip",
) -> tuple[str, int]:
    """Stream a binary from upstream directly into object storage.

    Returns (sha256_hex, size_bytes).
    """
    async with httpx.AsyncClient(follow_redirects=True) as client:
        async with client.stream("GET", url, timeout=300.0) as resp:
            resp.raise_for_status()
            stream = HashingStream(resp)
            await storage.put_stream(key, stream, content_type=content_type)
            return stream.sha256_hex, stream.size
