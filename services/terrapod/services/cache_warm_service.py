"""Cache pre-population (warming) — shared routine for the declarative warm
manifest (run once on API startup via a deduped scheduler trigger) and the
bulk-warm admin endpoint.

Warming pulls binaries and provider platforms into the cache ahead of time so a
fresh install — or an air-gapped one seeded from an egress-capable machine —
comes up with a populated cache instead of fetching lazily on first run. The
routine is resilient: one entry failing never aborts the rest; every attempt is
reported back so callers (the Job logs, the UI) can show exactly what landed and
what didn't.
"""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import WarmBinaryEntry, WarmPlatform, WarmProviderEntry, settings
from terrapod.logging_config import get_logger
from terrapod.services import binary_cache_service, provider_cache_service
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

# Fallback warm platforms for binary entries that don't list their own — the
# two platforms runner Jobs actually run on.
DEFAULT_WARM_PLATFORMS: list[WarmPlatform] = [
    WarmPlatform(os="linux", arch="amd64"),
    WarmPlatform(os="linux", arch="arm64"),
]


@dataclass
class WarmResult:
    """Outcome of warming one (artifact, platform) target."""

    kind: str  # "binary" | "provider"
    ref: str  # human-readable target, e.g. "terraform 1.12.3 linux/amd64"
    ok: bool
    error: str = ""


@dataclass
class WarmSummary:
    """Aggregate of a warm run."""

    results: list[WarmResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)


def _provider_default_platforms() -> list[WarmPlatform]:
    return [
        WarmPlatform(os=p["os"], arch=p["arch"]) for p in settings.registry.provider_cache.platforms
    ]


async def warm_from_manifest(
    db: AsyncSession,
    storage: ObjectStore,
    binaries: list[WarmBinaryEntry],
    providers: list[WarmProviderEntry],
) -> WarmSummary:
    """Pre-pull every listed binary + provider platform into the cache.

    Each (entry, platform) is warmed independently; a failure is captured in the
    returned summary and warming continues. Honours the per-cache `enabled`
    flags — a disabled cache marks its entries failed with a clear reason rather
    than silently skipping (so the caller sees why nothing landed).
    """
    summary = WarmSummary()

    binary_enabled = settings.registry.binary_cache.enabled
    for entry in binaries:
        platforms = entry.platforms or DEFAULT_WARM_PLATFORMS
        for plat in platforms:
            ref = f"{entry.tool} {entry.version} {plat.os}/{plat.arch}"
            if not binary_enabled:
                summary.results.append(
                    WarmResult("binary", ref, ok=False, error="binary cache is disabled")
                )
                continue
            try:
                await binary_cache_service.warm_binary(
                    db, storage, entry.tool, entry.version, plat.os, plat.arch
                )
                # Commit per entry so each success persists independently and a
                # later failure can't roll back already-warmed entries.
                await db.commit()
                summary.results.append(WarmResult("binary", ref, ok=True))
                logger.info("Warmed binary", ref=ref)
            except Exception as e:  # noqa: BLE001 — collect, never abort the batch
                await db.rollback()  # clear the aborted transaction for the next entry
                summary.results.append(WarmResult("binary", ref, ok=False, error=str(e)))
                logger.warning("Failed to warm binary", ref=ref, error=str(e))

    provider_enabled = settings.registry.provider_cache.enabled
    for entry in providers:
        hostname, namespace, type_ = entry.coordinates
        platforms = entry.platforms or _provider_default_platforms()
        for plat in platforms:
            ref = f"{entry.source} {entry.version} {plat.os}/{plat.arch}"
            if not provider_enabled:
                summary.results.append(
                    WarmResult("provider", ref, ok=False, error="provider cache is disabled")
                )
                continue
            try:
                await provider_cache_service.fetch_and_cache_single_platform(
                    db, storage, hostname, namespace, type_, entry.version, plat.os, plat.arch
                )
                await db.commit()
                summary.results.append(WarmResult("provider", ref, ok=True))
                logger.info("Warmed provider", ref=ref)
            except Exception as e:  # noqa: BLE001 — collect, never abort the batch
                await db.rollback()
                summary.results.append(WarmResult("provider", ref, ok=False, error=str(e)))
                logger.warning("Failed to warm provider", ref=ref, error=str(e))

    return summary


async def warm_manifest_task(payload: dict | None = None) -> None:
    """Scheduler trigger handler: warm the declarative manifest from settings.

    Fired once (deduped via Redis) shortly after API startup so a fresh install
    — or one whose manifest changed on upgrade — comes up with a populated
    cache, without a separate Job duplicating the API's runtime environment.
    Idempotent: already-cached entries are cheap DB hits, so re-firing on a
    later restart is a safe no-op. Self-gates when the manifest is empty.
    """
    from terrapod.db.session import get_db_session
    from terrapod.storage import get_storage

    binaries = settings.registry.binary_cache.warm
    providers = settings.registry.provider_cache.warm
    if not binaries and not providers:
        return

    logger.info(
        "Warming cache from declarative manifest",
        binaries=len(binaries),
        providers=len(providers),
    )
    storage = get_storage()
    async with get_db_session() as db:
        summary = await warm_from_manifest(db, storage, binaries, providers)
    logger.info(
        "Cache manifest warm complete",
        total=summary.total,
        succeeded=summary.succeeded,
        failed=summary.failed,
    )
