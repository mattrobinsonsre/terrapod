"""Is the object store actually there? (#1147, first slice of #1114)

The dangerous state a leader/follower pair can reach is **rows present, blobs
absent**: a promoted node that believes it has four hundred workspaces and cannot
serve one. Nothing looks wrong. The workspace list renders, the run history is
there, the registry lists every module — and then someone queues a run and
`terraform init` 404s, several layers away from the cause.

That state is cheap to detect, which is why this comes before any copying: **the
database row already names the key**, so the check is a HEAD. This turns "we
think the bucket replicated" into evidence.

It is worth having whoever does the replicating. Under provider-native bucket
replication it catches a misconfigured prefix or a lifecycle rule that expired
objects out from under the rows; under Terrapod-side copying it catches a class
that never ran.

Three properties this file is careful about:

**It never claims more than it checked.** A sample is reported as a sample, with
the numbers, and `missing == 0` on a sample is stated as *no missing objects among
those sampled* — not as "ready". Reporting readiness off fifty spot checks out of
forty thousand objects would be exactly the false confidence the check exists to
remove.

**It is bounded.** A full verify over an estate's state history is thousands of
round trips. Sampling with an explicit cap is the default; full is opt-in; both
report what they cost.

**It is a read-only observation.** Safe on either role, holds no DB session for
its duration, and does its per-object work concurrently but with a ceiling, so it
cannot saturate the store or starve a live leader.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.storage import keys

logger = structlog.get_logger(__name__)

#: How many objects a class is sampled down to when not verifying in full.
DEFAULT_SAMPLE = 25

#: Concurrent presence checks in flight. Enough to be quick on a remote store,
#: low enough that a readiness check never looks like a load test.
_CONCURRENCY = 8

#: What losing a class costs. The tiering is from #1114, and the important part
#: is that it is a property of the DEPLOYMENT, not only of the artifact: a cold
#: provider cache re-warms itself on first use — unless the node is sealed
#: (`cache_only`), in which case it can never run anything again and belongs in
#: the same tier as state.
IRREPLACEABLE = "irreplaceable"
HISTORY = "history"
REDERIVABLE = "rederivable"


@dataclass(frozen=True)
class ClassReadiness:
    """The result for one prefix class."""

    name: str
    tier: str
    #: Rows the database holds for this class. None when it was not counted
    #: (a class can be skipped without being unknown).
    total_rows: int = 0
    checked: int = 0
    missing: int = 0
    #: A few examples, for an operator to go and look at. Never the full list —
    #: a class that is entirely absent would otherwise produce a wall of keys.
    missing_examples: list[str] = field(default_factory=list)
    #: True when every row for this class was checked, so `missing` is complete.
    complete: bool = False
    #: Set when the class could not be checked at all, e.g. the store rejected
    #: the request. Distinct from "checked and found nothing missing".
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """No missing objects *among those checked*.

        Deliberately not called `ready`: on a sample this is the weaker claim,
        and the distinction is the whole point. `complete` is what says whether
        it is the strong one.
        """
        return self.error is None and self.missing == 0


@dataclass(frozen=True)
class BlobReadiness:
    classes: list[ClassReadiness] = field(default_factory=list)
    sampled: bool = True
    duration_ms: int = 0
    unavailable_reason: str | None = None

    @property
    def missing_total(self) -> int:
        return sum(c.missing for c in self.classes)

    @property
    def irreplaceable_missing(self) -> list[str]:
        """Classes in the irreplaceable tier with something missing.

        This is the list that should stop a failover. Everything else is a
        judgement call; this is not.
        """
        return [c.name for c in self.classes if c.tier == IRREPLACEABLE and c.missing > 0]


# ---------------------------------------------------------------------------
# Resolving keys from rows
#
# Each resolver returns (total_rows, keys_to_check). The split matters: a class
# can have ten thousand rows and be sampled down to twenty-five keys, and the
# report has to carry both numbers or the reader cannot tell how much of the
# class the answer covers.
# ---------------------------------------------------------------------------


async def _resolve_state(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Every state version, not only the latest.

    Rollback is a shipped feature, so a node holding only HEAD has silently lost
    rollback depth — and would look perfectly healthy doing it.
    """
    from terrapod.db.models import StateVersion

    total = await _count(db, StateVersion)
    stmt = select(StateVersion.workspace_id, StateVersion.id).order_by(
        StateVersion.created_at.desc()
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.state_key(str(ws), str(sv)) for ws, sv in rows]


async def _resolve_config_versions(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """The sharpest omission in the whole store.

    A VCS-connected workspace can refetch its configuration. A CLI-uploaded,
    catalog-provisioned or migrated one cannot — this tarball is the only copy.
    Losing it means those workspaces can never run again, while the UI still
    lists them as healthy.
    """
    from terrapod.db.models import ConfigurationVersion

    total = await _count(db, ConfigurationVersion)
    stmt = select(ConfigurationVersion.workspace_id, ConfigurationVersion.id).where(
        ConfigurationVersion.status == "uploaded"
    )
    stmt = stmt.order_by(ConfigurationVersion.created_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.config_version_key(str(ws), str(cv)) for ws, cv in rows]


async def _resolve_modules(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Module tarballs. The rows say the module exists, so the registry *looks*
    fine and every `terraform init` fails."""
    from terrapod.db.models import RegistryModule, RegistryModuleVersion

    total = await _count(db, RegistryModuleVersion)
    stmt = (
        select(
            RegistryModule.namespace,
            RegistryModule.name,
            RegistryModule.provider,
            RegistryModuleVersion.version,
        )
        .join(RegistryModuleVersion, RegistryModuleVersion.module_id == RegistryModule.id)
        .order_by(RegistryModuleVersion.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return total, [keys.module_tarball_key(ns, n, p, v) for ns, n, p, v in rows]


async def _resolve_provider_binaries(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """Provider platform zips, plus the signed manifest that makes them
    installable.

    Provider versions are client-signed and immutable: they cannot be
    regenerated server-side, only re-published by whoever holds the signing key.
    That makes a missing one closer to state than to a cache.
    """
    from terrapod.db.models import (
        RegistryProvider,
        RegistryProviderPlatform,
        RegistryProviderVersion,
    )

    total = await _count(db, RegistryProviderPlatform)
    stmt = (
        select(
            RegistryProvider.namespace,
            RegistryProvider.name,
            RegistryProviderVersion.version,
            RegistryProviderPlatform.os,
            RegistryProviderPlatform.arch,
        )
        .join(
            RegistryProviderVersion,
            RegistryProviderVersion.provider_id == RegistryProvider.id,
        )
        .join(
            RegistryProviderPlatform,
            RegistryProviderPlatform.version_id == RegistryProviderVersion.id,
        )
        .order_by(RegistryProviderPlatform.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()

    out: list[str] = []
    for ns, name, version, os_, arch in rows:
        out.append(keys.provider_binary_key(ns, name, version, os_, arch))
        # The manifest and its signature are what make the binary installable;
        # a present zip with an absent SHA256SUMS still fails `terraform init`.
        out.append(keys.provider_shasums_key(ns, name, version))
        out.append(keys.provider_shasums_sig_key(ns, name, version))
    return total, out


async def _resolve_state_index(db: AsyncSession, limit: int | None) -> tuple[int, list[str]]:
    """The break-glass recovery index.

    Worse than absent if it is stale on the promoted node: it points at objects
    that are not there while looking authoritative. Presence is all this checks —
    freshness would need the index parsed, which is a separate job.
    """
    from terrapod.db.models import StateVersion

    total = 1 if await _count(db, StateVersion) else 0
    return total, [keys.state_index_key()] if total else []


#: Registered classes, in the order #1114 sets out: irreplaceable first, because
#: that is the order a human reads them in and the order a copier should use.
RESOLVERS: list[
    tuple[str, str, Callable[[AsyncSession, int | None], Awaitable[tuple[int, list[str]]]]]
] = [
    ("state", IRREPLACEABLE, _resolve_state),
    ("state_index", IRREPLACEABLE, _resolve_state_index),
    ("configuration_versions", IRREPLACEABLE, _resolve_config_versions),
    ("registry_modules", IRREPLACEABLE, _resolve_modules),
    ("registry_providers", IRREPLACEABLE, _resolve_provider_binaries),
]


async def _count(db: AsyncSession, model: type) -> int:
    from sqlalchemy import func

    return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


async def _check_keys(store, blob_keys: Sequence[str]) -> tuple[int, list[str]]:
    """Presence-check keys concurrently, with a ceiling.

    Returns (missing_count, up_to_five_examples). A store error on one key is
    counted as missing rather than raised: the caller wants a readout, and one
    unreadable object should not lose the other nine hundred results.
    """
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    missing: list[str] = []

    async def one(key: str) -> None:
        async with semaphore:
            try:
                present = await store.exists(key)
            except Exception:
                logger.debug("blob readiness: presence check failed", key=key, exc_info=True)
                present = False
            if not present:
                missing.append(key)

    await asyncio.gather(*(one(k) for k in blob_keys))
    return len(missing), sorted(missing)[:5]


async def check(*, full: bool = False, sample: int = DEFAULT_SAMPLE) -> BlobReadiness:
    """Verify that the objects this node's rows name are actually present.

    `full=True` checks every row of every class — thousands of round trips on a
    real estate, which is why it is opt-in. Otherwise each class is sampled to
    `sample` newest rows, and the result says so.

    Never raises: an unreachable store is a readout with a reason, because the
    caller is an operator deciding whether to fail over and an exception tells
    them less than "I could not look".
    """
    from terrapod.db.session import get_db_session
    from terrapod.storage import get_storage

    started = time.monotonic()

    try:
        store = get_storage()
    except Exception as exc:  # noqa: BLE001 — a readout, not a failure
        return BlobReadiness(unavailable_reason=f"object store unavailable: {exc}")

    limit = None if full else max(1, sample)
    results: list[ClassReadiness] = []

    for name, tier, resolver in RESOLVERS:
        # A short-lived session per class rather than one held across the whole
        # check: the presence checks are the slow part, and holding a pooled
        # connection through them is how an observability feature starves the
        # thing it is observing.
        try:
            async with get_db_session() as db:
                total, blob_keys = await resolver(db, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("blob readiness: could not resolve class", cls=name, exc_info=True)
            results.append(ClassReadiness(name=name, tier=tier, error=str(exc)))
            continue

        missing, examples = await _check_keys(store, blob_keys)
        results.append(
            ClassReadiness(
                name=name,
                tier=tier,
                total_rows=total,
                checked=len(blob_keys),
                missing=missing,
                missing_examples=examples,
                # Complete when nothing was held back. Note this compares KEYS to
                # ROWS deliberately loosely — a provider platform row yields
                # three keys — so it is only claimed when no limit applied.
                complete=limit is None,
            )
        )

    readiness = BlobReadiness(
        classes=results,
        sampled=not full,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    if readiness.irreplaceable_missing:
        logger.warning(
            "blob readiness: irreplaceable objects missing",
            classes=readiness.irreplaceable_missing,
            missing_total=readiness.missing_total,
            sampled=readiness.sampled,
        )

    return readiness
