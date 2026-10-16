"""get_stream releases its reader thread when the consumer stops early (#2107).

The GCS backend reads the blob in a thread and hands chunks to the consumer
through a bounded queue. A consumer that stopped reading part-way (a closed
generator, a cancelled task, a client hanging up on a streamed download) left
that thread blocked on the queue for good, holding the open reader, and the
generator's cleanup waiting on the thread for good as well.
"""

from __future__ import annotations

import asyncio
import functools
import queue
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from terrapod.storage import gcs
from terrapod.storage.gcs import GCSStore
from terrapod.storage.protocol import InstrumentedStore

CHUNK = 64 * 1024
# How long a stream may stay in use before the fakes below fail it. Well past
# what any assertion waits; see gcs_queue.
_LIFETIME = 10.0
# How long a put must have waited on a full queue to count as stuck.
_STUCK_FOR = 0.2
_STILL_IN_USE = "the stream is still in use; nothing stopped it"


class _Reader:
    """An endless blob reader that records the thread reading it and whether
    it was closed. With `hold_after`, reads after that many wait for `release`.
    It fails once in use for `_LIFETIME`, so a thread that never stops reading
    ends."""

    def __init__(self, hold_after: int | None = None) -> None:
        self.thread: threading.Thread | None = None
        self.closed = threading.Event()
        self.holding = threading.Event()
        self.release = threading.Event()
        self._reads = 0
        self._hold_after = hold_after
        self._deadline = time.monotonic() + _LIFETIME

    def read(self, n: int) -> bytes:
        self.thread = threading.current_thread()
        if time.monotonic() > self._deadline:
            raise AssertionError(_STILL_IN_USE)
        self._reads += 1
        if self._hold_after is not None and self._reads > self._hold_after:
            self.holding.set()
            self.release.wait(_LIFETIME)
        return b"x" * n

    def close(self) -> None:
        self.closed.set()

    def __enter__(self) -> _Reader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _store(monkeypatch: pytest.MonkeyPatch, reader: _Reader) -> GCSStore:
    blob = MagicMock()
    blob.exists.return_value = True
    blob.open.return_value = reader
    client = MagicMock()
    client.bucket.return_value.blob.return_value = blob
    store = GCSStore(bucket="test-bucket", prefix="")
    monkeypatch.setattr(store, "_get_sync_client", lambda: client)
    return store


async def _wait_until(condition: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def _assert_released(reader: _Reader) -> None:
    assert await _wait_until(reader.closed.is_set), "the reader was left open"
    assert await _wait_until(lambda: not reader.thread.is_alive()), "the thread is still running"


@pytest.fixture(autouse=True)
def gcs_queue(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace the backend's queue with one that fails rather than stalls, and
    that can tell when a put is stuck on it.

    Failing: any put or get fails once the queue has been in use for
    `_LIFETIME`, timed or not. A stream that never stops would otherwise stall
    the run, since the loop waits for the thread at teardown.

    Stuck: `put_stuck(since)` is true once a put has waited on the full queue
    for `_STUCK_FOR`, all of it after `since`. That is the state the fix must
    interrupt, so a test reaches it before stopping the consumer; a put that
    merely found the queue full may still complete.
    """
    waiting_since: list[float] = []

    class _Queue(queue.Queue):
        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize)
            self._deadline = time.monotonic() + _LIFETIME

        def _bounded(self, wait, empty_or_full, block, timeout):  # type: ignore[no-untyped-def]
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(_STILL_IN_USE)
            if not block:
                return wait(False, None)
            try:
                return wait(True, remaining if timeout is None else min(timeout, remaining))
            except empty_or_full:
                if timeout is None or timeout >= remaining:
                    raise AssertionError(_STILL_IN_USE) from None
                raise

        def put(self, item, block=True, timeout=None):  # type: ignore[no-untyped-def]
            put = functools.partial(super().put, item)
            if not (block and self.full()):
                return self._bounded(put, queue.Full, block, timeout)
            started = time.monotonic()
            waiting_since.append(started)
            try:
                return self._bounded(put, queue.Full, block, timeout)
            finally:
                waiting_since.remove(started)

        def get(self, block=True, timeout=None):  # type: ignore[no-untyped-def]
            return self._bounded(super().get, queue.Empty, block, timeout)

    monkeypatch.setattr(
        gcs, "queue", SimpleNamespace(Queue=_Queue, Full=queue.Full, Empty=queue.Empty)
    )

    def put_stuck(since: float) -> bool:
        now = time.monotonic()
        return any(now - max(started, since) >= _STUCK_FOR for started in list(waiting_since))

    return SimpleNamespace(put_stuck=put_stuck)


async def test_closing_the_stream_early_releases_the_reader(
    monkeypatch: pytest.MonkeyPatch, gcs_queue: SimpleNamespace
):
    reader = _Reader()
    stream = _store(monkeypatch, reader).get_stream("k", chunk_size=CHUNK)

    assert await anext(stream) == b"x" * CHUNK
    stopped_reading = time.monotonic()
    assert await _wait_until(lambda: gcs_queue.put_stuck(stopped_reading))
    await asyncio.wait_for(stream.aclose(), timeout=5)

    await _assert_released(reader)


async def test_a_read_cancelled_while_waiting_leaves_no_executor_worker_behind(
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancelled while waiting for a chunk the thread has not read yet. The
    wait runs in an executor worker that cancelling the await does not stop,
    so with a one-worker executor a wait left behind blocks every later
    `to_thread` call, storage or not."""
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    reader = _Reader(hold_after=1)
    stream = _store(monkeypatch, reader).get_stream("k", chunk_size=CHUNK)

    async def consume() -> None:
        async for _ in stream:
            pass

    task = asyncio.create_task(consume())
    assert await _wait_until(reader.holding.is_set)
    task.cancel()
    await asyncio.sleep(0)  # let the cancellation reach the stream before the thread reads on
    reader.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    await _assert_released(reader)
    assert await asyncio.wait_for(asyncio.to_thread(lambda: "ran"), timeout=3) == "ran"


async def test_a_client_hanging_up_on_a_streamed_download_releases_the_reader(
    monkeypatch: pytest.MonkeyPatch, gcs_queue: SimpleNamespace
):
    """A real HTTP download, cut off part-way by the client, through the
    `InstrumentedStore` the API wraps every backend in."""
    reader = _Reader()
    store = InstrumentedStore(_store(monkeypatch, reader))

    async def download(request: Request) -> StreamingResponse:
        return StreamingResponse(store.get_stream("k", chunk_size=CHUNK))

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(Starlette(routes=[Route("/", download)]), lifespan="off", log_level="error")
    )
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        assert await _wait_until(lambda: server.started)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
        async with httpx.AsyncClient() as client, client.stream("GET", url) as response:
            assert response.status_code == 200
            # Held rather than left by `break`, which would let the iterator be
            # collected and close the connection before the thread is waiting.
            body = response.aiter_raw()
            assert await anext(body)
            stopped_reading = time.monotonic()
            assert await _wait_until(lambda: gcs_queue.put_stuck(stopped_reading))
        # Leaving the block closed the connection with most of the body unread.

        await _assert_released(reader)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=10)
        sock.close()
