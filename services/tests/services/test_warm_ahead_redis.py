"""`warm_ahead`'s Redis path, driven rather than patched away (#1420).

**Every warm route answered 500 and nothing in the suite noticed**, because
`warm_ahead` called `get_redis()` -- the FastAPI dependency, an `async def` with
a `yield` -- as if it were an accessor. That returns an async generator, so
`await redis.set(...)` raised `AttributeError: 'async_generator' object has no
attribute 'set'` on submit and `.get` on the job-status read. The whole
warm-ahead feature was dead: no job could be created and none could be read.

It survived because the tests patched `create_job`, `get_job` and `_save` -- the
three functions that contained the bug -- so the two lines that were wrong were
executed by nothing. This file is the opposite: it patches ONLY the accessor and
lets the real functions run, so the mistake cannot come back. Calling
`get_redis()` again would hand these a generator and fail here immediately.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from terrapod.services import warm_ahead
from terrapod.services.warm_ahead import WarmItem

pytestmark = pytest.mark.asyncio


class _FakeRedis:
    """Enough of the client for this module, and no more.

    Deliberately NOT an `AsyncMock`: a mock answers every attribute, so it would
    accept an async generator's API too and the test would pass under the bug.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        assert isinstance(key, str) and isinstance(value, str)
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


class TestTheRedisPathIsReal:
    async def test_a_job_round_trips_through_the_real_save_and_read(self):
        fake = _FakeRedis()
        with patch.object(warm_ahead, "get_redis_client", return_value=fake):
            job = await warm_ahead.create_job([WarmItem(ecosystem="pypi", name="six")])
            assert job.status == "queued"
            assert job.total == 1

            # One key written, and it is JSON -- so `_save` ran for real.
            assert len(fake.store) == 1
            payload = json.loads(next(iter(fake.store.values())))
            assert payload["job_id"] == job.job_id

            read = await warm_ahead.get_job(job.job_id)
        assert read is not None, "the job could not be read back"
        assert read.job_id == job.job_id
        assert read.status == "queued"
        assert read.total == 1

    async def test_an_unknown_job_is_none_rather_than_an_error(self):
        fake = _FakeRedis()
        with patch.object(warm_ahead, "get_redis_client", return_value=fake):
            assert await warm_ahead.get_job("warm-does-not-exist") is None


class TestTheDependencyIsNeverCalledAsAnAccessor:
    """The class, not the instance: no module may call `get_redis()` as a value.

    `get_redis` is a FastAPI dependency and the ONLY correct use is
    `Depends(get_redis)`. Everything else wants `get_redis_client()`, which 122
    call sites already do. Derived from the tree rather than listing modules, so
    a third offender fails here rather than in production.
    """

    def test_no_module_calls_the_dependency_as_a_value(self):
        import ast
        import pathlib

        root = pathlib.Path(warm_ahead.__file__).resolve().parents[1]
        offenders: list[str] = []
        scanned = 0
        for path in sorted(root.rglob("*.py")):
            text = path.read_text()
            if "get_redis" not in text:
                continue
            scanned += 1
            tree = ast.parse(text)
            # A `Depends(get_redis)` reference is a bare Name, never a Call, so
            # walking Call nodes separates the legitimate use from the mistake.
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name != "get_redis":
                    continue
                # The definition's own `yield get_redis_client()` is a different
                # call; only a CALL OF `get_redis` itself is wrong.
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        assert scanned >= 5, (
            f"only {scanned} module(s) mention get_redis; the walk has stopped finding them"
        )
        assert offenders == [], (
            "get_redis is the FastAPI dependency -- these call it as a value and will "
            f"get an async generator; use get_redis_client(): {offenders}"
        )
