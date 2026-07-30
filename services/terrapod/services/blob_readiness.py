"""Is the object store actually there? (#1147 and #1151, slices of #1114)

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

Which classes it looks at, and what tier each sits in, come from
:mod:`terrapod.services.blob_classes` — the same register the copier reads, so the
two can never disagree about what the store contains. A class configured ``off`` is
**reported as skipped**, never quietly dropped: a clean report has to distinguish
"nothing missing" from "nobody looked".

Three properties this file is careful about:

**It never claims more than it checked.** A sample is reported as a sample, with
the numbers, and `missing == 0` on a sample is stated as *no missing objects among
those sampled* — not as "ready". Reporting readiness off fifty spot checks out of
forty thousand objects would be exactly the false confidence the check exists to
remove. The same honesty covers classes that are not verifiable from rows at all:
they are listed, with the reason, rather than left out where their absence would
read as a pass.

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
from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog

from terrapod.services.blob_classes import (
    CLASSES,
    HISTORY,
    IRREPLACEABLE,
    OFF,
    REDERIVABLE,
    effective_mode,
    effective_tier,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "CLASSES",
    "DEFAULT_SAMPLE",
    "HISTORY",
    "IRREPLACEABLE",
    "REDERIVABLE",
    "BlobReadiness",
    "ClassReadiness",
    "check",
]

#: How many objects a class is sampled down to when not verifying in full.
DEFAULT_SAMPLE = 25

#: Concurrent presence checks in flight. Enough to be quick on a remote store,
#: low enough that a readiness check never looks like a load test.
_CONCURRENCY = 8


@dataclass(frozen=True)
class ClassReadiness:
    """The result for one prefix class."""

    name: str
    #: Tier on *this* deployment — sealing escalates a cache to irreplaceable,
    #: because a sealed node cannot re-warm one.
    tier: str
    #: off | verify | copy, as configured for this class.
    mode: str = "verify"
    #: Rows the database holds for this class. Zero when it was not counted (a
    #: class can be skipped without being unknown).
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
    #: False when no row guarantees these objects exist, so presence cannot be
    #: derived from the database. A boundary of the method, stated rather than
    #: hidden by omitting the class.
    verifiable: bool = True
    #: Why nothing was checked, when nothing was.
    note: str = ""

    @property
    def healthy(self) -> bool:
        """No missing objects *among those checked*.

        Deliberately not called `ready`: on a sample this is the weaker claim,
        and the distinction is the whole point. `complete` is what says whether
        it is the strong one, and `checked` whether there was a claim at all.
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

    @property
    def irreplaceable_unchecked(self) -> list[str]:
        """Irreplaceable classes this run made no claim about.

        The counterpart to the list above, and the reason it can be trusted. A
        class that is off, or not verifiable from rows, produces zero missing
        objects — which looks identical to a pass unless it is named. On a sealed
        node this is where the escalated caches surface.
        """
        return [
            c.name
            for c in self.classes
            if c.tier == IRREPLACEABLE and c.checked == 0 and c.error is None
        ]


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

    for cls in CLASSES:
        tier = effective_tier(cls)
        mode = effective_mode(cls)

        if mode == OFF:
            results.append(
                ClassReadiness(
                    name=cls.name,
                    tier=tier,
                    mode=mode,
                    verifiable=cls.resolver is not None,
                    note="configured off, so nothing was checked",
                )
            )
            continue

        if cls.resolver is None:
            results.append(
                ClassReadiness(
                    name=cls.name,
                    tier=tier,
                    mode=mode,
                    verifiable=False,
                    note=cls.unverifiable_reason,
                )
            )
            continue

        # A short-lived session per class rather than one held across the whole
        # check: the presence checks are the slow part, and holding a pooled
        # connection through them is how an observability feature starves the
        # thing it is observing.
        try:
            async with get_db_session() as db:
                total, blob_keys = await cls.resolver(db, limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("blob readiness: could not resolve class", cls=cls.name, exc_info=True)
            results.append(ClassReadiness(name=cls.name, tier=tier, mode=mode, error=str(exc)))
            continue

        missing, examples = await _check_keys(store, blob_keys)
        results.append(
            ClassReadiness(
                name=cls.name,
                tier=tier,
                mode=mode,
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
