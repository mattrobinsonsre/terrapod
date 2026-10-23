"""Reading a configure's per-host results (#1928).

Driven as requests rather than by calling the helpers, because the properties
under test belong to the routes: the union with the resolved-inventory artifact,
and that read on the workspace is what it takes.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import RunHostEvent, RunHostResult
from terrapod.db.session import get_db

_BASE = "http://test"


def _user():
    return AuthenticatedUser(
        email="someone@example.com",
        display_name="Someone",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _run(ws_id):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = ws_id
    return run


def _result(host, **kw):
    row = RunHostResult(
        host=host,
        ok=kw.get("ok", 1),
        changed=kw.get("changed", 0),
        failures=kw.get("failures", 0),
        unreachable=kw.get("unreachable", 0),
        skipped=kw.get("skipped", 0),
        rescued=0,
        ignored=kw.get("ignored", 0),
        saw_unreachable=kw.get("saw_unreachable", False),
    )
    row.id = uuid.uuid4()
    row.created_at = row.updated_at = None
    return row


def _app(db):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: db
    return app


async def _get(app, path):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url=_BASE) as c:
        return await c.get(path, headers={"Authorization": "Bearer t"})


def _storage_with(hosts: list[str]):
    """A storage whose resolved-inventory artifact names these hosts."""
    document = {"hosts": {h: {} for h in hosts}, "groups": {}, "children": {}}
    storage = MagicMock()
    storage.get = AsyncMock(return_value=json.dumps(document).encode())
    return storage


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@pytest.mark.asyncio
class TestTheHostResultsRead:
    async def _drive(self, rows, targeted, *, db=None):
        ws_id = uuid.uuid4()
        run = _run(ws_id)
        db = db or AsyncMock()
        db.get = AsyncMock(return_value=run)
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch("terrapod.api.routers.runs._require_run_ws_capability", AsyncMock()),
            patch(
                "terrapod.services.host_results_service.list_results",
                AsyncMock(return_value=rows),
            ),
            patch("terrapod.storage.get_storage", return_value=_storage_with(targeted)),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-results")
        return resp

    async def test_it_returns_a_row_per_reported_host(self, *_mocks):
        resp = await self._drive([_result("web1"), _result("web2")], ["web1", "web2"])
        assert resp.status_code == 200
        hosts = [e["attributes"]["host"] for e in resp.json()["data"]]
        assert hosts == ["web1", "web2"]
        assert all(e["attributes"]["reached"] for e in resp.json()["data"])

    async def test_a_targeted_host_ansible_never_mentioned_is_shown_as_not_reached(self, *_mocks):
        """The measured gap, and the reason the union exists.

        With `serial:` and `any_errors_fatal:` the recap holds only the batch
        that ran, so rendering what ansible reported would show a four-host run
        as a clean one-host success.
        """
        resp = await self._drive([_result("web1")], ["web1", "web2", "db1"])
        entries = {e["attributes"]["host"]: e["attributes"] for e in resp.json()["data"]}
        assert set(entries) == {"web1", "db1", "web2"}
        assert entries["web2"]["status"] == "not-reached"
        assert entries["web2"]["reached"] is False
        # null rather than zero: zero is a measurement, this is its absence.
        assert entries["web2"]["ok"] is None

    async def test_status_comes_off_saw_unreachable_not_the_counters(self, *_mocks):
        resp = await self._drive(
            [_result("hdead", ok=2, unreachable=0, ignored=1, saw_unreachable=True)],
            ["hdead"],
        )
        attrs = resp.json()["data"][0]["attributes"]
        assert attrs["unreachable"] == 0, "the fixture's premise"
        assert attrs["status"] == "unreachable"

    async def test_a_failure_outranks_a_change_in_the_derived_status(self, *_mocks):
        resp = await self._drive([_result("web2", changed=3, failures=1)], ["web2"])
        assert resp.json()["data"][0]["attributes"]["status"] == "failed"

    async def test_an_unreadable_inventory_artifact_degrades_to_what_was_reported(self, *_mocks):
        """A read must not fail because the denominator is missing."""
        ws_id = uuid.uuid4()
        run = _run(ws_id)
        db = AsyncMock()
        app = _app(db)
        broken = MagicMock()
        broken.get = AsyncMock(side_effect=RuntimeError("gone"))
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch("terrapod.api.routers.runs._require_run_ws_capability", AsyncMock()),
            patch(
                "terrapod.services.host_results_service.list_results",
                AsyncMock(return_value=[_result("web1")]),
            ),
            patch("terrapod.storage.get_storage", return_value=broken),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-results")
        assert resp.status_code == 200
        assert [e["attributes"]["host"] for e in resp.json()["data"]] == ["web1"]

    async def test_it_carries_the_house_pagination_meta(self, *_mocks):
        resp = await self._drive([_result("web1")], ["web1"])
        meta = resp.json()["meta"]["pagination"]
        assert set(meta) == {"current-page", "page-size", "total-count", "total-pages"}


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@pytest.mark.asyncio
class TestTheHostEventsRead:
    @staticmethod
    def _event(host, kind="failed", **kw):
        row = RunHostEvent(
            host=host,
            kind=kind,
            task_name=kw.get("task_name", "a task"),
            task_action="ansible.builtin.command",
            task_uuid="u",
            play_name="p",
            ignored=kw.get("ignored", False),
            msg=kw.get("msg", "boom"),
            payload_ref=kw.get("payload_ref"),
            seq=kw.get("seq", 1),
        )
        row.id = uuid.uuid4()
        row.created_at = None
        return row

    async def _drive(self, rows, query=""):
        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch("terrapod.api.routers.runs._require_run_ws_capability", AsyncMock()),
            patch(
                "terrapod.services.host_results_service.list_events",
                AsyncMock(return_value=rows),
            ) as listed,
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-events{query}")
        return resp, listed

    async def test_it_returns_the_failure_events(self, *_mocks):
        resp, _ = await self._drive([self._event("web2")])
        assert resp.status_code == 200
        attrs = resp.json()["data"][0]["attributes"]
        assert attrs["host"] == "web2"
        assert attrs["kind"] == "failed"
        assert attrs["task-name"] == "a task"
        assert attrs["message"] == "boom"

    async def test_the_host_filter_reaches_the_service(self, *_mocks):
        _resp, listed = await self._drive([], query="?host=web2")
        assert listed.await_args.kwargs["host"] == "web2"

    async def test_an_absent_filter_is_not_passed_as_an_empty_host(self, *_mocks):
        """`host=""` would filter to a host whose name is empty, i.e. nothing."""
        _resp, listed = await self._drive([])
        assert listed.await_args.kwargs["host"] is None

    async def test_a_spilled_payload_reference_is_surfaced(self, *_mocks):
        resp, _ = await self._drive([self._event("web2", payload_ref=7)])
        assert resp.json()["data"][0]["attributes"]["payload-ref"] == 7


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@pytest.mark.asyncio
class TestTheEventStreamSeparatesReapedFromNeverWritten:
    """#1928: "where retention has expired the detail, the UI SAYS SO".

    Three cases, and the middle one is the whole point. Both absent cases
    present identically at the storage layer -- a missing object -- so a test
    that instantiates only one of them passes whichever status the route
    happens to return, which is the recorded trap: when the property is a
    RELATIONSHIP between two states, the test must instantiate both.
    """

    @staticmethod
    def _storage(*, present: bool):
        storage = MagicMock()
        storage.exists = AsyncMock(return_value=present)
        storage.presigned_get_url = AsyncMock(return_value=MagicMock(url="https://x/y"))
        return storage

    @pytest.mark.parametrize("suffix", ["stream", "payloads"])
    async def test_present_redirects(self, _db, _redis, _storage_p, suffix):
        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.api.routers.runs._require_run_ws_capability",
                AsyncMock(return_value=None),
            ),
            patch(
                "terrapod.api.routers.runs.get_storage",
                return_value=self._storage(present=True),
            ),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-events/{suffix}")
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://x/y"

    async def test_reaped_is_410_and_says_retention_removed_it(self, _db, _redis, _storage_p):
        """The acceptance clause. A 404 here reads as "nothing happened"."""
        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.api.routers.runs._require_run_ws_capability",
                AsyncMock(return_value=None),
            ),
            patch(
                "terrapod.api.routers.runs.get_storage",
                return_value=self._storage(present=False),
            ),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_results",
                AsyncMock(return_value=[_result("web1")]),
            ),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-events/stream")
        assert resp.status_code == 410
        assert "retention" in resp.json()["detail"]

    async def test_never_written_is_404_not_410(self, _db, _redis, _storage_p):
        """No recap rows means no stream was ever stored, so nothing was lost."""
        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.api.routers.runs._require_run_ws_capability",
                AsyncMock(return_value=None),
            ),
            patch(
                "terrapod.api.routers.runs.get_storage",
                return_value=self._storage(present=False),
            ),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_results",
                AsyncMock(return_value=[]),
            ),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-events/stream")
        assert resp.status_code == 404
        assert "retention" not in resp.json()["detail"]

    async def test_a_host_is_served_its_OWN_object_not_a_filter_of_the_stream(
        self, _db, _redis, _storage_p
    ):
        """The partition is the point of #1928's "one object fetch".

        Mutation target: serving `host_events_key` here regardless of `?host=`
        passes every status assertion while making the drill-down a scan of a
        stream that is tens of MB at fleet scale -- the exact cost the partition
        exists to remove, and invisible to any test that checks only the code.
        """
        from terrapod.storage.keys import host_events_host_key

        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        storage = self._storage(present=True)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.api.routers.runs._require_run_ws_capability",
                AsyncMock(return_value=None),
            ),
            patch("terrapod.api.routers.runs.get_storage", return_value=storage),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-events/stream?host=web1")
        assert resp.status_code == 302
        asked = storage.exists.await_args.args[0]
        assert asked == host_events_host_key(str(run.workspace_id), str(run.id), "web1"), (
            f"served the wrong object: {asked}"
        )

    async def test_an_unknown_host_is_404_while_a_reaped_one_is_410(self, _db, _redis, _storage_p):
        """Narrowed to the host, not the run.

        A run that reported ten hosts and is asked about an eleventh lost
        nothing, so it must not claim retention took something. Both cases are
        driven, because each alone passes whichever status the route returns.
        """
        run = _run(uuid.uuid4())
        for host, rows, expect in (
            ("web1", [_result("web1")], 410),
            ("nope", [_result("web1")], 404),
        ):
            db = AsyncMock()
            app = _app(db)
            with (
                patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
                patch(
                    "terrapod.api.routers.runs._require_run_ws_capability",
                    AsyncMock(return_value=None),
                ),
                patch(
                    "terrapod.api.routers.runs.get_storage",
                    return_value=self._storage(present=False),
                ),
                patch(
                    "terrapod.api.routers.runs.host_results_service.list_results",
                    AsyncMock(return_value=rows),
                ),
            ):
                resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-events/stream?host={host}")
            assert resp.status_code == expect, f"host={host} gave {resp.status_code}"

    async def test_the_payloads_sidecar_derives_from_a_payload_ref(self, _db, _redis, _storage_p):
        """A run that reported hosts but spilled nothing LOST nothing.

        The sidecar and the stream are reaped together and written
        independently, so reading the sidecar's existence off the stream's
        would report a retention event for a run that never spilled.
        """
        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.api.routers.runs._require_run_ws_capability",
                AsyncMock(return_value=None),
            ),
            patch(
                "terrapod.api.routers.runs.get_storage",
                return_value=self._storage(present=False),
            ),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_results",
                AsyncMock(return_value=[_result("web1")]),
            ),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_events",
                AsyncMock(return_value=[RunHostEvent(host="web1", payload_ref=None)]),
            ),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/host-events/payloads")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestTheSummaryReadReportsWhetherTheDetailSurvives:
    """`meta.detail.state`, which is what lets the UI "say so" (#1928).

    Driven at `_detail_state` rather than through the route, because the four
    states are a property of that function and the route only forwards it --
    and one of the four (`unknown`) cannot be produced through the route
    without making a storage backend raise.
    """

    @staticmethod
    async def _state(*, reported: bool, exists=None, raises=False):
        from terrapod.api.routers import runs as mod

        run = _run(uuid.uuid4())
        storage = MagicMock()
        storage.exists = AsyncMock(
            side_effect=RuntimeError("backend down") if raises else None,
            return_value=exists,
        )
        with patch.object(mod, "get_storage", return_value=storage):
            return await mod._detail_state(run, reported)

    async def test_no_host_reported_is_none_without_touching_storage(self):
        from terrapod.api.routers import runs as mod

        run = _run(uuid.uuid4())
        storage = MagicMock()
        storage.exists = AsyncMock(return_value=True)
        with patch.object(mod, "get_storage", return_value=storage):
            assert await mod._detail_state(run, False) == "none"
        storage.exists.assert_not_awaited()

    async def test_object_present_is_available(self):
        assert await self._state(reported=True, exists=True) == "available"

    async def test_object_absent_with_rows_is_reaped(self):
        """The acceptance case: the summary outlives the artifact."""
        assert await self._state(reported=True, exists=False) == "reaped"

    async def test_a_broken_backend_is_unknown_never_none(self):
        """`none` is a claim; `unknown` is the honest answer.

        Mutation target: collapsing this to `none` makes the panel tell an
        operator the run has no per-task detail when the truth is that nobody
        could ask. That is the recorded failure of reporting an absent signal
        as a passing one, in a message a human reads.
        """
        assert await self._state(reported=True, raises=True) == "unknown"


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@pytest.mark.asyncio
class TestTheStreamIsStateGradeNotRunGrade:
    """The tier is the security property, and it is not visible behaviourally.

    The callback writes `result._result` verbatim for every event including
    `ok`, so the object carries each module's whole return value. Serving that
    at `run:read` is GHSA-gwwq-5v7q-h3f4 exactly -- a full resolved document one
    tier below raw state. Both routes are asserted, because the sidecar holds
    the LARGEST payloads the run produced.
    """

    @pytest.mark.parametrize("suffix", ["stream", "payloads"])
    async def test_the_artifact_routes_demand_state_read(self, _db, _redis, _storage_p, suffix):
        from terrapod.auth import capabilities as cap

        seen = []

        async def _record(run, capability, user, db, request=None):
            seen.append(capability)

        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        storage = MagicMock()
        storage.exists = AsyncMock(return_value=False)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch("terrapod.api.routers.runs._require_run_ws_capability", _record),
            patch("terrapod.api.routers.runs.get_storage", return_value=storage),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_results",
                AsyncMock(return_value=[]),
            ),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_events",
                AsyncMock(return_value=[]),
            ),
        ):
            await _get(app, f"/api/v1/runs/run-{run.id}/host-events/{suffix}")

        assert seen == [cap.STATE_READ], (
            f"the {suffix} artifact must be gated on state:read, not {seen}"
        )

    @pytest.mark.parametrize("suffix", ["host-results", "host-events"])
    async def test_the_summary_routes_stay_at_run_read(self, _db, _redis, _storage_p, suffix):
        """The other direction, so the fix for the above cannot over-correct.

        Raising these to `state:read` would withhold a configure's outcome from
        the read tier that is meant to see it -- the counters, and a failure
        message that is ansible's own text truncated, which is the configure
        equivalent of a plan log.
        """
        from terrapod.auth import capabilities as cap

        seen = []

        async def _record(run, capability, user, db, request=None):
            seen.append(capability)

        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch("terrapod.api.routers.runs._require_run_ws_capability", _record),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_results",
                AsyncMock(return_value=[]),
            ),
            patch(
                "terrapod.api.routers.runs.host_results_service.list_events",
                AsyncMock(return_value=[]),
            ),
            patch("terrapod.api.routers.runs.get_storage", return_value=MagicMock()),
            patch(
                "terrapod.api.routers.runs._targeted_hosts",
                AsyncMock(return_value=set()),
            ),
        ):
            await _get(app, f"/api/v1/runs/run-{run.id}/{suffix}")

        assert seen == [cap.RUN_READ], f"{suffix} must stay at run:read, not {seen}"


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@pytest.mark.asyncio
class TestBothReadsRequireReadOnTheWorkspace:
    @pytest.mark.parametrize("suffix", ["host-results", "host-events"])
    async def test_the_capability_is_checked(self, _db, _redis, _storage, suffix):
        """Mutation target: deleting the check leaves both reads open.

        A configure's per-host outcome names every host in a fleet, so it is no
        more public than the run's log.
        """
        from fastapi import HTTPException

        run = _run(uuid.uuid4())
        db = AsyncMock()
        app = _app(db)
        with (
            patch("terrapod.api.routers.runs._get_run", AsyncMock(return_value=run)),
            patch(
                "terrapod.api.routers.runs._require_run_ws_capability",
                AsyncMock(side_effect=HTTPException(status_code=403, detail="no")),
            ),
        ):
            resp = await _get(app, f"/api/v1/runs/run-{run.id}/{suffix}")
        assert resp.status_code == 403
