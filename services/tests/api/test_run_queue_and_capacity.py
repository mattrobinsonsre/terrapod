"""The organization run queue and capacity routes (#2378).

`docs/tfe-cli-surface.md` catalogued both as in-scope and neither was served. A
404 from either is **fatal** upstream, not cosmetic: `waitForRun` does
`return r, generalError("Failed to retrieve queue", err)`, so the plan or apply
aborts rather than losing a "position in queue" message.

And it was reachable, which #2378 first said it was not. `confirmed` is a real
`tfe.RunStatus`, so Terrapod reports it verbatim, and `waitForRun` returns early
only on a status that is neither `pending` nor `confirmed` -- so right after
`Runs.Apply` the CLI waits `backoffMin` = 1.000s, reads the run, and if it is
still `confirmed` walks into the queue-position block. Measured on a dev stack:
confirm -> `applying` took 0.305s and 0.555s, so `tofu apply` worked by a ~450ms
margin; with the listener scaled to zero the run held in `confirmed` for the
whole 20s window, and there the apply aborts.

The route-level properties that matter, and why each is here rather than left to
the contract snapshot (which pins that a path exists, not what it does):

* `position-in-queue` is emitted, and is the position in the WHOLE queue. Drop
  it and `position` stays 0 upstream, `ReadCapacity` is never reached, and the
  CLI prints "waiting for the operation to start" -- the route serving perfectly
  and doing nothing. The attribute contract cannot see it: it is added by a
  helper the serializer calls, which that gate's own docstring lists as a known
  blind spot, so this file is its only pin.
* which runs are LISTED is filtered by `run:read`, and the positions are not.
* both routes describe the same population, because the CLI subtracts one from
  the other.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
QUEUE = "/api/tfe/v2/organizations/default/runs/queue"
CAPACITY = "/api/tfe/v2/organizations/default/capacity"
#: The native doors to the same two answers (#2425 §4.4). Not under `/runs/`:
#: neither is a run resource, and `/runs/queue` resolved to `show_run` with
#: `run_id="queue"` because `app.py` includes `router` before
#: `extensions_router` and FastAPI matches in declaration order.
NATIVE_QUEUE = "/api/v1/run-queue"
NATIVE_CAPACITY = "/api/v1/run-capacity"


def _user(roles=None):
    return AuthenticatedUser(
        email="queue@example.test",
        display_name="Queue",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _ws(ws_id=None, engine="terraform"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = "queue-ws"
    ws.engine = engine
    ws.labels = {}
    ws.owner_email = "someone-else@example.test"
    return ws


def _run(ws_id, status="queued", *, is_configure=False, after_apply=False):
    """A run with EVERY column set to a JSON-safe value, derived from the model.

    Not a hand-written field list. `_run_json` serialises a wide row, and an
    attribute a fixture forgets arrives as an auto-MagicMock that reaches
    `json.dumps` and raises `TypeError: Object of type MagicMock is not JSON
    serializable` -- naming the attribute but not the fixture, and leaving the
    next person to add columns one failing run at a time. Walking
    `Run.__table__.columns` means a column added later is covered the day it
    lands.
    """
    from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text

    from terrapod.db.models import Run as RunModel

    run = MagicMock()
    for column in RunModel.__table__.columns:
        kind = column.type
        if isinstance(kind, Boolean):
            value: object = False
        elif isinstance(kind, (Integer, Numeric)):
            value = 0
        elif isinstance(kind, DateTime):
            value = datetime(2026, 1, 1, tzinfo=UTC)
        elif isinstance(kind, (String, Text)):
            value = ""
        else:
            # JSONB and anything else: None serialises, a MagicMock does not.
            value = None
        setattr(run, column.name, value)

    run.id = uuid.uuid4()
    run.workspace_id = ws_id
    run.status = status
    run.is_configure = is_configure
    run.configure_after_apply = after_apply
    run.message = "queued run"
    run.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    # Nullable timestamps and ids stay None: a run that has not planned has no
    # `plan_started_at`, and the serializer branches on exactly that.
    for nullable in (
        "plan_started_at",
        "plan_finished_at",
        "apply_started_at",
        "apply_finished_at",
        "configure_started_at",
        "configuration_version_id",
        "pool_id",
        "plan_id",
        "apply_id",
        "has_changes",
        "error_message",
        "discard_reason",
        "target_addrs",
        "replace_addrs",
        "run_variables",
    ):
        if hasattr(run, nullable):
            setattr(run, nullable, None)
    return run


def _app(user, queue=None, capacity=(0, 0), caps=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    db = AsyncMock()
    db.add = MagicMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


def _patches(queue, capacity, caps, *, workspaces=None):
    """Patch at the boundary below the routes: the two queries and the resolver."""
    ws_by_id = {w.id: w for w in (workspaces or [])}

    async def _get(model, key):
        return ws_by_id.get(key)

    return (
        patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
        patch("terrapod.api.app.init_redis"),
        patch("terrapod.api.app.init_db"),
        patch(
            "terrapod.api.routers.tfe_v2.run_service.organization_run_queue",
            AsyncMock(return_value=queue),
        ),
        patch(
            "terrapod.api.routers.tfe_v2.run_service.organization_run_capacity",
            AsyncMock(return_value=capacity),
        ),
        patch(
            "terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for",
            AsyncMock(side_effect=caps),
        ),
        patch("terrapod.api.routers.tfe_v2.fetch_custom_roles", AsyncMock(return_value=[])),
        _get,
    )


async def _get_json(app, db, path, queue, capacity, caps, workspaces):
    p = _patches(queue, capacity, caps, workspaces=workspaces)
    db.get = AsyncMock(side_effect=p[-1])
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(path, headers=_AUTH)
    return resp


class TestTheQueue:
    async def test_the_position_is_emitted(self):
        """The attribute the whole feature turns on, and the one the attribute
        contract cannot see."""
        ws = _ws()
        queue = [(_run(ws.id), 1), (_run(ws.id), 2), (_run(ws.id), 3)]
        app, db = _app(_user())
        resp = await _get_json(
            app, db, QUEUE, queue, (3, 0), lambda *a, **k: caps_for_level("read"), [ws]
        )
        assert resp.status_code == 200, resp.text
        positions = [d["attributes"]["position-in-queue"] for d in resp.json()["data"]]
        assert positions == [1, 2, 3]

    async def test_the_position_is_over_the_whole_queue_not_the_visible_slice(self):
        """A run the caller CAN see keeps its true position when the runs ahead
        of it are on workspaces it cannot. The number is the point of the route;
        counted over the filtered list it would understate the wait it reports."""
        hidden, mine = _ws(), _ws()
        queue = [(_run(hidden.id), 1), (_run(hidden.id), 2), (_run(mine.id), 3)]
        app, db = _app(_user())

        def caps(db_, user, ws, **kw):
            return caps_for_level("read") if ws is mine else frozenset()

        resp = await _get_json(app, db, QUEUE, queue, (3, 0), caps, [hidden, mine])
        data = resp.json()["data"]
        assert len(data) == 1, "only the readable run is listed"
        assert data[0]["attributes"]["position-in-queue"] == 3, "and its position is TRUE"

    async def test_a_run_on_an_unreadable_workspace_is_not_listed(self):
        ws = _ws()
        app, db = _app(_user())
        resp = await _get_json(
            app, db, QUEUE, [(_run(ws.id), 1)], (1, 0), lambda *a, **k: frozenset(), [ws]
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert body["meta"]["pagination"]["total-count"] == 0

    async def test_read_is_the_capability_not_bare_truthiness(self):
        """`run:read` explicitly. A role holding some other capability on the
        workspace -- a pool or registry grant that leaves the workspace axis
        empty of run access -- must not see the queue."""
        ws = _ws()
        app, db = _app(_user())
        resp = await _get_json(
            app,
            db,
            QUEUE,
            [(_run(ws.id), 1)],
            (1, 0),
            lambda *a, **k: frozenset({"workspace:read"}),
            [ws],
        )
        assert resp.json()["data"] == []

    async def test_the_capabilities_are_resolved_once_per_workspace(self):
        """Memoised: a queue of runs on one workspace must not be one resolution
        each -- that is the O(fleet) shape #1056 forbids."""
        ws = _ws()
        queue = [(_run(ws.id), i) for i in range(1, 6)]
        app, db = _app(_user())
        resolver = AsyncMock(return_value=caps_for_level("read"))
        p = _patches(queue, (5, 0), lambda *a, **k: caps_for_level("read"), workspaces=[ws])
        db.get = AsyncMock(side_effect=p[-1])
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for", resolver),
            p[6],
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(QUEUE, headers=_AUTH)
        assert len(resp.json()["data"]) == 5
        assert resolver.await_count == 1, "five runs, one workspace, one resolution"

    async def test_an_empty_queue_is_an_empty_list_not_a_404(self):
        """`waitForRun` turns any error into a fatal `Failed to retrieve queue`,
        so "nothing is queued" has to be a 200 with no items."""
        app, db = _app(_user())
        resp = await _get_json(app, db, QUEUE, [], (0, 0), lambda *a, **k: frozenset(), [])
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    async def test_the_unpaged_default_reports_one_page(self):
        """go-tfe's `ListOptions` is `omitempty`, so its first call sends no page
        params and takes the unpaged path. It then loops while
        `CurrentPage < TotalPages`, setting `PageNumber = NextPage` -- a key
        Terrapod's meta deliberately does not emit. One page is what keeps that
        loop terminating, and it is why no `next-page` is needed."""
        ws = _ws()
        queue = [(_run(ws.id), i) for i in range(1, 8)]
        app, db = _app(_user())
        resp = await _get_json(
            app, db, QUEUE, queue, (7, 0), lambda *a, **k: caps_for_level("read"), [ws]
        )
        page = resp.json()["meta"]["pagination"]
        assert page["current-page"] == 1
        assert page["total-pages"] == 1
        assert len(resp.json()["data"]) == 7

    async def test_a_queued_run_reports_a_status_go_tfe_knows(self):
        """The queue document carries the TFE vocabulary, so `queued` -- which is
        not a `tfe.RunStatus` -- must come out folded (#2378)."""
        from terrapod.api.routers.runs import _TFE_STATUS_FOLD

        assert _TFE_STATUS_FOLD["queued"] == "plan_queued"
        ws = _ws()
        app, db = _app(_user())
        resp = await _get_json(
            app,
            db,
            QUEUE,
            [(_run(ws.id, "queued"), 1)],
            (1, 0),
            lambda *a, **k: caps_for_level("read"),
            [ws],
        )
        assert resp.json()["data"][0]["attributes"]["status"] == "plan_queued"

    async def test_a_standalone_configure_reports_apply_queued(self):
        """The fold is by PHASE, which is why it is not a bare dict lookup.

        Terrapod's `queued` means "waiting for a runner to claim it", and the
        dispatcher claims an ordinary run as a plan and a standalone configure as
        a configure. `apply_queued` is the honest TFE word for the second,
        because a configure mutates hosts -- the same reading that already folds
        `configuring` to `applying`. Driven through the route, not through the
        dict, so removing the branch fails here.
        """
        ws = _ws()
        app, db = _app(_user())
        resp = await _get_json(
            app,
            db,
            QUEUE,
            [(_run(ws.id, "queued", is_configure=True, after_apply=False), 1)],
            (1, 0),
            lambda *a, **k: caps_for_level("read"),
            [ws],
        )
        assert resp.json()["data"][0]["attributes"]["status"] == "apply_queued"

    async def test_a_composite_reports_plan_queued(self):
        """A composite is `is_configure=True` AND `configure_after_apply=True`,
        and it DOES plan first -- so it is not the standalone case. Asserted
        because `run.is_configure` alone is the wrong predicate here for exactly
        the reason `is_standalone_configure` exists (#1974)."""
        ws = _ws()
        app, db = _app(_user())
        resp = await _get_json(
            app,
            db,
            QUEUE,
            [(_run(ws.id, "queued", is_configure=True, after_apply=True), 1)],
            (1, 0),
            lambda *a, **k: caps_for_level("read"),
            [ws],
        )
        assert resp.json()["data"][0]["attributes"]["status"] == "plan_queued"


class TestTheCapacity:
    async def test_it_reports_pending_and_running(self):
        app, db = _app(_user())
        resp = await _get_json(app, db, CAPACITY, [], (4, 2), lambda *a, **k: frozenset(), [])
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["type"] == "organization-capacity"
        assert data["id"] == "default"
        assert data["attributes"] == {"pending": 4, "running": 2}

    async def test_the_counts_are_ints_not_strings(self):
        """`tfe.Capacity` types both as `int`; a string would fail to unmarshal
        and the apply would abort on `Failed to retrieve capacity`."""
        app, db = _app(_user())
        resp = await _get_json(app, db, CAPACITY, [], (0, 0), lambda *a, **k: frozenset(), [])
        attrs = resp.json()["data"]["attributes"]
        assert isinstance(attrs["pending"], int) and isinstance(attrs["running"], int)

    async def test_it_is_not_filtered_by_reach(self):
        """Deliberately unfiltered, and asserted so nobody "tightens" it: the CLI
        subtracts `running` from a queue position computed over the whole queue,
        so a per-caller count would describe a different population from the
        number it is subtracted from. Two integers about load, no identities."""
        app, db = _app(_user())
        resolver = AsyncMock(side_effect=AssertionError("capacity must not resolve capabilities"))
        p = _patches([], (9, 3), lambda *a, **k: frozenset())
        db.get = AsyncMock(side_effect=p[-1])
        with (
            p[0],
            p[1],
            p[2],
            p[3],
            p[4],
            patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for", resolver),
            p[6],
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(CAPACITY, headers=_AUTH)
        assert resp.json()["data"]["attributes"] == {"pending": 9, "running": 3}
        assert not resolver.called


class TestBothAreOnTheCompatibilitySurfaceOnly:
    def test_neither_is_mounted_natively(self):
        """A native mount would carry an `organizations/default` segment, which
        architecture principle 9 forbids and
        `test_no_native_route_anywhere_carries_an_org_segment` refuses. So this
        is forced rather than chosen -- and worth pinning, because the obvious
        "serve it natively too" would fail in a different file."""
        from terrapod.api.app import app
        from terrapod.api.prefixes import NATIVE_LEGACY_PREFIX, NATIVE_PREFIX

        paths = {getattr(r, "path", "") for r in app.routes}
        for prefix in (NATIVE_PREFIX, NATIVE_LEGACY_PREFIX):
            for suffix in ("/organizations/default/runs/queue", "/organizations/default/capacity"):
                assert prefix + suffix not in paths

    def test_both_are_mounted_on_the_compatibility_surface(self):
        from terrapod.api.app import app
        from terrapod.api.prefixes import TFE_LEGACY_PREFIX, TFE_PREFIX

        paths = {getattr(r, "path", "") for r in app.routes}
        for prefix in (TFE_PREFIX, TFE_LEGACY_PREFIX):
            for suffix in ("/organizations/default/runs/queue", "/organizations/default/capacity"):
                assert prefix + suffix in paths, f"{prefix}{suffix} is not served"


class TestTheNativeDoorsServeTheSameTwoAnswers:
    """#2425 §4.4: the queue was reachable only through `/api/tfe/v2`.

    So the `terraform`/`tofu` cloud backend could tell a waiting practitioner how
    far down the queue they were, and nothing else could -- an agent asked "why
    has my run not started" could list runs and not see the queue at all. The
    recorded reason was *"a native path would carry an `organizations/default`
    segment, which Terrapod does not have"*, which is true of the PATH and not of
    the data: the counts and positions carry no identity, as the TFE route's own
    docstring says.

    **The population is identical by construction**, through the one
    `visible_run_queue` producer, and that is the property these tests exist to
    hold. Two routes over two different populations would give the same run two
    different positions depending on which you asked, and the engine filter --
    which makes the queue Terraform-only -- is in the shared query rather than in
    either route.
    """

    async def test_the_native_queue_emits_the_same_positions(self):
        ws = _ws()
        queue = [(_run(ws.id), 1), (_run(ws.id), 2), (_run(ws.id), 3)]
        app, db = _app(_user())
        resp = await _get_json(
            app, db, NATIVE_QUEUE, queue, (3, 0), lambda *a, **k: caps_for_level("read"), [ws]
        )
        assert resp.status_code == 200, resp.text
        assert [d["attributes"]["position-in-queue"] for d in resp.json()["data"]] == [1, 2, 3]

    async def test_the_native_queue_is_filtered_by_reach_too(self):
        """The filter is the shared producer's, so this is really asserting that
        the native route goes through it rather than re-querying."""
        hidden, mine = _ws(), _ws()
        queue = [(_run(hidden.id), 1), (_run(hidden.id), 2), (_run(mine.id), 3)]
        app, db = _app(_user())

        def caps(db_, user, ws, **kw):
            return caps_for_level("read") if ws.id == mine.id else frozenset()

        resp = await _get_json(app, db, NATIVE_QUEUE, queue, (3, 0), caps, [hidden, mine])
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert len(data) == 1, data
        # Position 3, not 1: the reach filter decides what is LISTED and the
        # position is over the whole queue, on this surface as on the other.
        assert data[0]["attributes"]["position-in-queue"] == 3

    async def test_the_native_queue_reports_the_REAL_status(self):
        """The one deliberate difference between the two documents.

        A `queued` run folds to `plan_queued` or `apply_queued` on the frozen
        surface, because `go-tfe` switches on a fixed set. Natively it must be
        the real status, which is the #1988 split -- and the route derives the
        surface from the request rather than hardcoding it, so this would also
        hold if it were ever dual-mounted.
        """
        ws = _ws()
        queue = [(_run(ws.id, status="queued"), 1)]
        app, db = _app(_user())
        native = await _get_json(
            app, db, NATIVE_QUEUE, queue, (1, 0), lambda *a, **k: caps_for_level("read"), [ws]
        )
        app2, db2 = _app(_user())
        frozen = await _get_json(
            app2, db2, QUEUE, queue, (1, 0), lambda *a, **k: caps_for_level("read"), [ws]
        )
        assert native.json()["data"][0]["attributes"]["status"] == "queued"
        folded = frozen.json()["data"][0]["attributes"]["status"]
        assert folded != "queued", (
            "the frozen surface must fold a status `go-tfe` cannot switch on; if it stops "
            f"folding, this test is no longer a contrast. Got {folded!r}"
        )

    async def test_the_native_capacity_reports_the_same_two_integers(self):
        app, db = _app(_user())
        resp = await _get_json(
            app, db, NATIVE_CAPACITY, [], (7, 2), lambda *a, **k: caps_for_level("read"), []
        )
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs == {"pending": 7, "running": 2}

    async def test_neither_native_route_carries_an_org_segment(self):
        """Architecture principle 9, and the reason these paths are not a
        dual-mount of the TFE ones. A `dual_router` entry would have put
        `organizations/default` on the native surface, which is the #1898 shape
        -- an org segment arriving as a side effect of a mount rather than as a
        path anyone wrote."""
        for path in (NATIVE_QUEUE, NATIVE_CAPACITY):
            assert "organizations" not in path, path

    async def test_the_queue_is_absent_from_the_frozen_surface(self):
        """On `extensions_router`, which is `include_terrapod` alone.

        `router` is mounted on BOTH surfaces, so declaring these there would
        have answered at `/api/tfe/v2/run-queue` as well -- a TFE path no CLI
        consumes, serving a document whose statuses are native.
        `test_the_two_native_callers_are_the_runner_protocol_ones` caught that
        when this was first written on `router`.
        """
        from terrapod.api.app import app as mounted

        paths = {r.path for r in mounted.routes if hasattr(r, "path")}
        assert "/api/v1/run-queue" in paths
        assert "/api/tfe/v2/run-queue" not in paths
        assert "/api/tfe/v2/run-capacity" not in paths

    async def test_the_native_path_resolves_to_the_native_handler(self):
        """Measured, because the first version did not.

        `/runs/queue` resolved to `show_run` with `run_id="queue"`: `app.py`
        includes `router` before `extensions_router`, FastAPI matches in
        declaration order, and `/runs/{run_id}` is on `router`. A comment of mine
        claimed the ordering held; asking the mounted app which route it matches
        is what settled it.
        """
        from fastapi.routing import APIRoute

        from terrapod.api.app import app as mounted

        for path, want in (
            (NATIVE_QUEUE, "native_run_queue"),
            (NATIVE_CAPACITY, "native_run_capacity"),
        ):
            hit = None
            for route in mounted.routes:
                if isinstance(route, APIRoute):
                    match, _ = route.matches(
                        {
                            "type": "http",
                            "method": "GET",
                            "path": path,
                            "headers": [],
                            "query_string": b"",
                        }
                    )
                    if str(match).endswith("FULL"):
                        hit = route.endpoint.__name__
                        break
            assert hit == want, f"{path} resolves to {hit}, not {want}"
