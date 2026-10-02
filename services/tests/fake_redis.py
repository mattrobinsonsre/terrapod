"""A Redis fake with real expiry, shared by the session and role-change tests.

An `AsyncMock` records calls; it cannot tell a clamped TTL from an unclamped one,
and it cannot answer "is this session still alive after thirty hours". The code
under test reads a record, recomputes a TTL and writes it back, so the tests need
a store that actually forgets things.

The clock is the test's: `advance()` moves key expiry forward, and the fixture
that installs this also moves the wall clock the code compares deadlines against.
Moving one without the other is how a test ends up proving nothing — Redis drops
a key the code still thinks is live, or the reverse.
"""

from __future__ import annotations

import time


class FakeRedis:
    """Enough Redis to expire a key: GET/SET/TTL/DEL plus the set operations.

    Expiry is evaluated against a clock the test controls (`advance`), because the
    behaviour under test is what happens hours into a session.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deadlines: dict[str, float] = {}
        self.sets: dict[str, set[str]] = {}
        self.offset = 0.0

    # -- clock -----------------------------------------------------------
    def now(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def _live(self, key: str) -> bool:
        deadline = self.deadlines.get(key)
        if deadline is not None and deadline <= self.now():
            self.values.pop(key, None)
            self.deadlines.pop(key, None)
            self.sets.pop(key, None)
            return False
        return key in self.values or key in self.sets

    # -- strings ---------------------------------------------------------
    async def get(self, key: str):
        return self.values.get(key) if self._live(key) else None

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False):
        if nx and self._live(key):
            return None
        self.values[key] = value
        if ex is not None:
            self.deadlines[key] = self.now() + ex
        return True

    async def ttl(self, key: str):
        if not self._live(key):
            return -2
        deadline = self.deadlines.get(key)
        if deadline is None:
            return -1
        return int(deadline - self.now())

    async def delete(self, *keys: str):
        removed = 0
        for key in keys:
            if self._live(key):
                removed += 1
            self.values.pop(key, None)
            self.sets.pop(key, None)
            self.deadlines.pop(key, None)
        return removed

    async def expire(self, key: str, seconds: int):
        if not self._live(key):
            return False
        self.deadlines[key] = self.now() + seconds
        return True

    async def scan_iter(self, match: str = "*", count: int = 100):
        prefix = match.rstrip("*")
        for key in list(self.values):
            if key.startswith(prefix) and self._live(key):
                yield key

    # -- sets ------------------------------------------------------------
    async def sadd(self, key: str, *members: str):
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    async def smembers(self, key: str):
        return set(self.sets.get(key, set())) if self._live(key) else set()

    async def srem(self, key: str, *members: str):
        bucket = self.sets.get(key, set())
        removed = len(bucket & set(members))
        self.sets[key] = bucket - set(members)
        return removed

    # -- pipeline --------------------------------------------------------
    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)


class _FakePipeline:
    """Queues the calls the real code queues, then applies them in order."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __getattr__(self, name: str):
        def queue(*args, **kwargs):
            self._queued.append((name, args, kwargs))
            return self

        return queue

    async def execute(self):
        results = []
        for name, args, kwargs in self._queued:
            results.append(await getattr(self._redis, name)(*args, **kwargs))
        self._queued.clear()
        return results
