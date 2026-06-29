"""Cache pre-population (warming) — the shared routine behind the bulk-warm
admin endpoint (and its UI panel).

Warming pulls binaries and provider platforms into the cache ahead of time so an
operator can seed an air-gapped (or just slow-first-run) install instead of
relying on lazy fetch-on-first-use. The routine is resilient: one entry failing
never aborts the rest; every attempt is reported back so the caller (the UI) can
show exactly what landed and what didn't.
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
