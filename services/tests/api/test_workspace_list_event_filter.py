"""The workspace-list SSE stream is filtered per subscriber (GHSA-mc7f-xmq4-jgvw).

`tp:workspace_list_events` is ONE global Redis channel carrying every
workspace's id and coarse status, and every authenticated user subscribes to it.
Before the filter that made the stream an inventory of the whole deployment,
RBAC notwithstanding.

These tests drive the real ASGI app — the full middleware stack, FastAPI routing
and the handler — rather than calling the filter helper, because the defect was
that the route did not consult a filter at all. A test that called
`_ReadableWorkspaces` directly would keep passing if the route stopped using it,
which is precisely the shape of guard this repository has shipped before.

Why a hand-rolled ASGI driver instead of `AsyncClient`: httpx's `ASGITransport`
buffers a response to completion before returning it, and an SSE stream never
completes — so `c.stream(...)` against this endpoint hangs without ever
delivering a status line. The driver below sends `http.disconnect` once it has
collected enough body chunks, which is exactly how a real client ends an SSE
subscription and is what the handler's `request.is_disconnected()` loop expects.
"""

import asyncio
import contextlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth.capabilities import caps_for_level

_PATH = "/api/terrapod/v1/workspace-events"


def _user(email="reader@example.com"):
    return AuthenticatedUser(
        email=email,
        display_name="Reader",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


class _FakePubSub:
    """Replays a fixed list of channel messages, then goes quiet.

    Quiet is `None`, which is what redis-py returns on timeout — the handler
    answers it with a keepalive comment, so the stream stays open exactly as it
    does in production.
    """

    def __init__(self, payloads):
        self._queue = [{"type": "message", "data": json.dumps(p).encode()} for p in payloads]
        self.unsubscribed = False

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if self._queue:
            return self._queue.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def unsubscribe(self, channel):
        self.unsubscribed = True

    async def aclose(self):
        return None


def _db_session_returning(workspaces):
    """A `get_db_session()` stand-in whose `db.get` resolves these workspaces.

    Keyed by workspace id, so a payload naming an id that is not here resolves to
    None — the deleted-workspace case, which must be treated as unreadable.
    """

    def factory():
        db = AsyncMock()

        async def _get(_model, ws_uuid):
            return workspaces.get(str(ws_uuid))

        db.get = AsyncMock(side_effect=_get)

        @contextlib.asynccontextmanager
        async def _cm():
            yield db

        return _cm()

    return factory


async def _subscribe(app, *, body_chunks=2, timeout=8.0):
    """Drive the app as a real ASGI client, disconnecting after N body chunks.

    Returns `(status, body)`. The disconnect is what lets the handler's loop
    break and the response finish; without it the stream runs for ever.
    """
    started: dict = {}
    chunks: list[bytes] = []
    disconnect = asyncio.Event()
    sent_request = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect.is_set():
            return {"type": "http.disconnect"}
        # Suspends, so Starlette's zero-timeout `is_disconnected()` poll reads it
        # as "still connected" — which is what a live subscription looks like.
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            started.update(message)
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                chunks.append(body)
            if len(chunks) >= body_chunks:
                disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": _PATH,
        "raw_path": _PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"authorization", b"Bearer dummy")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except TimeoutError:
        # The stream produced fewer chunks than asked for; end it and report
        # whatever arrived rather than wedging the suite.
        disconnect.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=3.0)
    return started.get("status"), b"".join(chunks).decode("utf-8", "replace")


def _mock_ws(ws_id, name):
    ws = MagicMock()
    ws.id = ws_id
    ws.name = name
    ws.labels = {}
    ws.owner_email = None
    ws.catalog_item_id = None
    return ws


def _patches(*, payloads, workspaces=None, caps=None, db_session=None):
    """The four seams every test here shares: who is asking, what the channel
    carries, how a workspace is loaded, and what capabilities it resolves to."""
    return (
        patch(
            "terrapod.api.dependencies.authenticate_request",
            new=AsyncMock(return_value=_user()),
        ),
        patch(
            "terrapod.redis.client.subscribe_channel",
            new=AsyncMock(return_value=_FakePubSub(payloads)),
        ),
        patch(
            "terrapod.db.session.get_db_session",
            new=db_session or _db_session_returning(workspaces or {}),
        ),
        patch(
            "terrapod.api.routers.workspace_extensions.resolve_workspace_capabilities_for",
            new=caps or AsyncMock(return_value=caps_for_level("read")),
        ),
    )


class TestTheWorkspaceListStreamIsFilteredPerSubscriber:
    async def test_an_event_for_an_unreadable_workspace_is_not_delivered(self):
        """The finding itself: a subscriber must not learn that a workspace it
        cannot read exists, nor what state it is in."""
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        workspaces = {
            str(mine): _mock_ws(mine, "mine"),
            str(theirs): _mock_ws(theirs, "theirs"),
        }

        async def _caps(_db, _user, ws):
            return caps_for_level("read") if ws.name == "mine" else frozenset()

        app = create_app()
        p = _patches(
            payloads=[
                {
                    "event": "run_status_change",
                    "workspace_id": str(theirs),
                    "new_status": "applying",
                },
                {"event": "workspace_updated", "workspace_id": str(mine)},
            ],
            workspaces=workspaces,
            caps=AsyncMock(side_effect=_caps),
        )
        with p[0], p[1], p[2], p[3]:
            status, body = await _subscribe(app)

        assert status == 200
        assert str(mine) in body, f"the readable workspace's event was dropped: {body!r}"
        assert str(theirs) not in body, (
            "an event for a workspace the subscriber cannot read was delivered — "
            f"the stream is an existence oracle again: {body!r}"
        )
        assert "applying" not in body

    async def test_a_workspace_that_no_longer_exists_is_treated_as_unreadable(self):
        """Fail closed. A payload naming a row that has gone cannot be scoped, so
        it is dropped rather than passed through for want of an answer."""
        gone = uuid.uuid4()
        app = create_app()
        p = _patches(
            payloads=[{"event": "workspace_updated", "workspace_id": str(gone)}],
            workspaces={},  # the row is not there
            caps=AsyncMock(return_value=caps_for_level("admin")),
        )
        with p[0], p[1], p[2], p[3]:
            _status, body = await _subscribe(app, body_chunks=1, timeout=4.0)

        assert str(gone) not in body

    async def test_a_payload_with_no_workspace_id_is_dropped(self):
        """An unscopeable payload cannot be filtered, so it must not be sent."""
        app = create_app()
        p = _patches(payloads=[{"event": "something_new", "detail": "unscoped"}])
        with p[0], p[1], p[2], p[3]:
            _status, body = await _subscribe(app, body_chunks=1, timeout=4.0)

        assert "something_new" not in body

    async def test_a_resolution_failure_drops_the_event(self):
        """A filter that leaks on a transient database error is a filter that
        leaks whenever it matters most."""
        ws_id = uuid.uuid4()

        def _exploding():
            raise RuntimeError("database is away")

        app = create_app()
        p = _patches(
            payloads=[{"event": "workspace_updated", "workspace_id": str(ws_id)}],
            db_session=_exploding,
        )
        with p[0], p[1], p[2], p[3]:
            _status, body = await _subscribe(app, body_chunks=1, timeout=4.0)

        assert str(ws_id) not in body

    async def test_one_decision_per_workspace_not_per_event(self):
        """The cache is load-bearing, not an optimisation to drop: a busy fleet
        publishes many events per workspace and each miss costs a DB session."""
        ws_id = uuid.uuid4()
        resolve = AsyncMock(return_value=caps_for_level("read"))

        app = create_app()
        p = _patches(
            payloads=[{"event": "workspace_updated", "workspace_id": str(ws_id)} for _ in range(5)],
            workspaces={str(ws_id): _mock_ws(ws_id, "mine")},
            caps=resolve,
        )
        with p[0], p[1], p[2], p[3]:
            _status, body = await _subscribe(app, body_chunks=5, timeout=8.0)

        assert body.count(str(ws_id)) == 5
        assert resolve.await_count == 1
