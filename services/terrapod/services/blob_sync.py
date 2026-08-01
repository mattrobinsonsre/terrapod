"""The follower's object-store copier (#960 phase 4, #1159).

Settings replication moves rows; this moves the objects those rows name. Same
direction and the same reason: **the follower pulls**, so a follower that is
down, slow, or throttled simply stops asking and the leader neither knows nor
cares. "A peer outage must never block a healthy leader" holds by construction.

Only classes the operator set to ``copy`` are touched (#1152), and they are
walked **irreplaceable first** — the classes whose loss is permanent go over the
link before the ones that re-derive, so a cycle that runs out of budget runs out
of it having done the part that mattered.

**Resumability falls out of being diff-driven.** Each key is checked against this
node's own store and skipped if present, so a cycle that dies halfway leaves the
copied objects copied and the next cycle re-diffs. There is no cursor to corrupt
and nothing to reconcile. The one hazard that needs care is a *partially written*
object looking present to the next `exists()`, which is why a copy that lands at
the wrong size is deleted rather than left to be mistaken for a good one.

**Bandwidth is measured, capped, and reported.** Copying an estate's state
history is not free, and the numbers say so: bytes and objects per class in the
metrics and in the log line. A cycle that stops because it hit its budget says
that too — a silent cap reads as "finished", which is the one thing it must not.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import structlog

from terrapod.config import settings
from terrapod.http_retry import arequest_with_retry
from terrapod.services import blob_classes
from terrapod.services.blob_classes import COPY, IRREPLACEABLE

logger = structlog.get_logger(__name__)

#: Listing and small requests. The content fetch gets its own, longer budget:
#: a multi-hundred-megabyte provider zip over a cross-region link legitimately
#: takes minutes, and timing it out mid-stream would retry from the start
#: forever.
_TIMEOUT = httpx.Timeout(30.0)
_CONTENT_TIMEOUT = httpx.Timeout(30.0, read=600.0)

#: Keys per listing page. Metadata only, so this can be generous — the diff is
#: the cheap half and keeping the page count low keeps the leader's listing cost
#: down.
_PAGE = 500


@dataclass
class ClassResult:
    """What one class cost, and what it achieved."""

    name: str
    tier: str
    examined: int = 0
    copied: int = 0
    skipped_present: int = 0
    bytes_copied: int = 0
    failed: int = 0
    #: Set when the class stopped before it was finished, with the reason. The
    #: field exists so "we ran out of budget" can never be reported as "done".
    stopped_early: str | None = None


@dataclass
class SyncResult:
    classes: list[ClassResult] = field(default_factory=list)
    duration_ms: int = 0
    skipped_reason: str | None = None

    @property
    def bytes_copied(self) -> int:
        return sum(c.bytes_copied for c in self.classes)

    @property
    def objects_copied(self) -> int:
        return sum(c.copied for c in self.classes)

    @property
    def stopped_early(self) -> list[str]:
        """Classes that did not finish. The honest counterpart to the totals: a
        big `objects_copied` with entries here is progress, not completion."""
        return [c.name for c in self.classes if c.stopped_early]


class _Throttle:
    """A byte budget spread over time, plus an optional hard ceiling per cycle.

    Deliberately crude — it sleeps off the excess after each object rather than
    pacing within one. Objects here range from a few KB to hundreds of MB, so
    smoothing inside a single transfer would be false precision; what matters is
    that a cycle's *average* stays under the ceiling the operator set, and that
    the leader is not asked for the next object while this one is still being
    paid for.
    """

    def __init__(self, bytes_per_second: int, bytes_per_cycle: int) -> None:
        self._rate = bytes_per_second
        self._cycle_cap = bytes_per_cycle
        self._spent = 0
        self._started = time.monotonic()

    @property
    def spent(self) -> int:
        return self._spent

    def cap_reached(self) -> bool:
        return self._cycle_cap > 0 and self._spent >= self._cycle_cap

    async def account(self, byte_count: int) -> None:
        """Record `byte_count` as spent, and wait if that ran ahead of the rate."""
        self._spent += byte_count
        if self._rate <= 0:
            return
        earned = time.monotonic() - self._started
        owed = self._spent / self._rate
        if owed > earned:
            await asyncio.sleep(owed - earned)


async def _peer_token(client: httpx.AsyncClient) -> str:
    """The peer access token, reusing the settings-replication cache.

    Shared rather than fetched separately: it is the same credential for the same
    peer, and two caches would mean two renewal clocks drifting apart.
    """
    from terrapod.services.replication_sync import _peer_token as shared

    return await shared(client)


async def _copy_one(
    client: httpx.AsyncClient,
    token: str,
    base: str,
    cls_name: str,
    key: str,
    expected_size: int,
) -> int:
    """Stream one object from the peer into this node's store.

    Returns the bytes written. Streamed end to end (rule 14): the object crosses
    the link and lands in storage without being held in memory at either end.
    """
    from terrapod.storage import get_storage

    store = get_storage()

    async with client.stream(
        "GET",
        f"{base}/api/terrapod/v1/ha/replication/blobs/{cls_name}/content",
        params={"key": key},
        headers={"Authorization": f"Bearer {token}"},
        timeout=_CONTENT_TIMEOUT,
    ) as resp:
        resp.raise_for_status()
        meta = await store.put_stream(key, resp.aiter_bytes())

    # A short write is worse than a failed one: the next cycle's `exists()` would
    # call it present and the corruption would outlive every retry. Removing it
    # puts the key back in the diff.
    if expected_size and meta.size_bytes != expected_size:
        await store.delete(key)
        raise ValueError(f"copied {meta.size_bytes} bytes for {key}, peer reported {expected_size}")
    return meta.size_bytes


async def _sync_class(
    client: httpx.AsyncClient,
    token: str,
    base: str,
    cls: blob_classes.BlobClass,
    throttle: _Throttle,
) -> ClassResult:
    result = ClassResult(name=cls.name, tier=blob_classes.effective_tier(cls))
    # Refuse to copy a class whose objects are enveloped by app-layer
    # encryption (#635). Each node wraps with ITS OWN data key and `crypto_keys`
    # is deliberately never replicated, so a byte-for-byte copy lands on the
    # peer as ciphertext it holds no key for. Every object would be present,
    # readiness would report a clean bill of health, and the failure would first
    # appear at failover as an AES-GCM tag error on every state file at once.
    #
    # Refusing is the honest answer: an empty class the operator can see beats a
    # full one they cannot decrypt. The column path is unaffected — it decrypts
    # on read and the peer re-encrypts on write; the blob path has no such step.
    if cls.encrypted_at_rest and settings.encryption.enabled:
        result.stopped_early = (
            "app-layer encryption is on and this class is enveloped per-node — "
            "copied bytes would be undecryptable on the peer"
        )
        logger.warning(
            "skipping blob class: encrypted per-node, a copy would not be readable",
            blob_class=cls.name,
        )
        return result

    from terrapod.storage import get_storage

    store = get_storage()
    cfg = settings.ha.blobs

    semaphore = asyncio.Semaphore(cfg.concurrency)
    cursor = ""

    while True:
        if throttle.cap_reached():
            result.stopped_early = "cycle byte cap reached"
            return result

        resp = await arequest_with_retry(
            client,
            "GET",
            f"{base}/api/terrapod/v1/ha/replication/blobs/{cls.name}",
            idempotent=True,
            params={"after": cursor, "limit": _PAGE},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        page = body["data"]
        result.examined += len(page)

        wanted: list[tuple[str, int]] = []
        for entry in page:
            key = entry["attributes"]["key"]
            if await store.exists(key):
                result.skipped_present += 1
                continue
            wanted.append((key, int(entry["attributes"]["size-bytes"])))

        async def one(key: str, size: int) -> None:
            async with semaphore:
                # Checked here as well as between pages: a single page can hold
                # 500 objects, so a cap only enforced at the page boundary would
                # be one the operator set and Terrapod overshot.
                if throttle.cap_reached():
                    result.stopped_early = "cycle byte cap reached"
                    return
                try:
                    written = await _copy_one(client, token, base, cls.name, key, size)
                except Exception:
                    # One unreadable object must not abandon the class: the rest
                    # of the estate is still worth copying, and the next cycle
                    # re-diffs this key anyway.
                    result.failed += 1
                    logger.warning("blob copy failed", cls=cls.name, key=key, exc_info=True)
                    return
                result.copied += 1
                result.bytes_copied += written
                await throttle.account(written)

        await asyncio.gather(*(one(k, s) for k, s in wanted))

        if result.stopped_early:
            return result
        if body["meta"]["complete"]:
            return result
        cursor = body["meta"]["cursor"]


async def sync_cycle() -> None:
    """One copy pass. Registered as a periodic task; the scheduler's claim
    already serialises it across replicas."""
    result = await run_cycle()

    if result.skipped_reason:
        return

    log = logger.info if result.objects_copied or result.stopped_early else logger.debug
    log(
        "blob copy cycle",
        objects_copied=result.objects_copied,
        bytes_copied=result.bytes_copied,
        duration_ms=result.duration_ms,
        # Named rather than left to be inferred from the totals: a cycle that
        # stopped at its budget has copied a lot AND is not finished, and only
        # one of those is visible in a byte count.
        stopped_early=result.stopped_early,
    )


async def run_cycle() -> SyncResult:
    """The cycle, as a value — so a caller (and a test) can see what it did."""
    cfg = settings.ha.blobs
    started = time.monotonic()

    copy_classes = [c for c in blob_classes.CLASSES if blob_classes.effective_mode(c) == COPY]
    if not copy_classes:
        # The default is `verify`, so this is the ordinary case on almost every
        # install: nothing is configured to copy, and there is nothing to do.
        return SyncResult(skipped_reason="no classes configured to copy")

    if not settings.ha.peer.url:
        return SyncResult(skipped_reason="no peer configured")

    # Irreplaceable first. `CLASSES` is already in that order, and sorting by the
    # EFFECTIVE tier means a sealed node's caches are promoted with it rather than
    # being left until last on the node where they are fatal.
    copy_classes.sort(key=lambda c: 0 if blob_classes.effective_tier(c) == IRREPLACEABLE else 1)

    throttle = _Throttle(cfg.max_bytes_per_second, cfg.max_bytes_per_cycle)
    base = settings.ha.peer.url.rstrip("/")
    result = SyncResult()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            token = await _peer_token(client)
        except Exception as exc:
            # Same rule as the settings puller: only drop the cached token when
            # the peer REJECTED it. A 429 or 5xx says nothing about validity, and
            # discarding it there guarantees another mint next cycle (#960).
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                from terrapod.services.replication_sync import reset_token_cache

                reset_token_cache()
            logger.warning("Could not authenticate to peer for blob copy", exc_info=True)
            return SyncResult(skipped_reason="peer authentication failed")

        for cls in copy_classes:
            try:
                result.classes.append(await _sync_class(client, token, base, cls, throttle))
            except Exception:
                # A class that cannot be listed at all is reported as failed and
                # the next class is attempted: one broken class must not stop
                # state from being copied.
                logger.warning("blob copy class failed", cls=cls.name, exc_info=True)
                result.classes.append(
                    ClassResult(
                        name=cls.name,
                        tier=blob_classes.effective_tier(cls),
                        stopped_early="class listing failed",
                    )
                )

    result.duration_ms = int((time.monotonic() - started) * 1000)
    _refresh_metrics(result)
    return result


def _refresh_metrics(result: SyncResult) -> None:
    from terrapod.api.metrics import (
        BLOB_COPY_BYTES,
        BLOB_COPY_FAILURES,
        BLOB_COPY_OBJECTS,
        BLOB_COPY_STOPPED_EARLY,
    )

    for cls in result.classes:
        if cls.copied:
            BLOB_COPY_OBJECTS.labels(blob_class=cls.name).inc(cls.copied)
        if cls.bytes_copied:
            BLOB_COPY_BYTES.labels(blob_class=cls.name).inc(cls.bytes_copied)
        if cls.failed:
            BLOB_COPY_FAILURES.labels(blob_class=cls.name).inc(cls.failed)

    BLOB_COPY_STOPPED_EARLY.set(len(result.stopped_early))


__all__ = ["ClassResult", "SyncResult", "run_cycle", "sync_cycle"]
