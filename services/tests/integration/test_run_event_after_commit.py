"""Integration: a run event is published only once its row is visible (#2720).

Real Postgres and two real sessions, because the defect is invisible to anything
less. `transition_run` wrote the status, flushed, and published — all inside the
transaction — so the event reached a subscriber before the row it described was
visible to any *other* session. A client that refetched on receipt read the
PREVIOUS status.

Whether that race is lost depends on who commits and when. An API request commits
in `get_db` as soon as the handler returns. The run reconciler commits once,
outside its per-run loop, so its window is every other in-flight run. A configure
is driven only by the reconciler — there is no `/configure-result` POST — and
nothing publishes after its terminal transition, so the stale read was permanent:
the badge sat on `configuring` until the page was reloaded by hand.

**A transaction's events all publish at its commit**, so an INTERMEDIATE event
can describe a status the row has already moved past -- `pending -> queued`
arrives while the row reads `planning`. That is correct and is the point: a client
refetching on receipt gets the truth rather than the event's claim. The bug was
reading *behind* an event, never ahead of it, which is why these assert that no
event is published while the row still reads the status it says we left.

**What these assert is the user-visible property, not the mechanism.** Each one
reads the row back from a SEPARATE session at the moment the event fires, which is
exactly what the browser's refetch does. A test that only checked "an event was
published" passes on the broken code — which is why the thirteen transition tests
that patch the publisher out never caught this.
"""

import asyncio
import json
import uuid as _uuid

import pytest
from sqlalchemy import select

from terrapod.db import post_commit
from terrapod.db.models import ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import run_events, run_service

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _hooks() -> None:
    """The listeners are installed in the app lifespan, which a test does not run."""
    post_commit.install_hooks()


async def _settle() -> None:
    """Await every scheduled side effect, rather than sleeping and hoping."""
    while post_commit._in_flight:
        await asyncio.gather(*list(post_commit._in_flight), return_exceptions=True)


async def _seed() -> tuple[Workspace, ConfigurationVersion]:
    async with get_db_session() as db:
        ws = Workspace(
            name=f"after-commit-{_uuid.uuid4().hex[:8]}",
            execution_mode="agent",
        )
        db.add(ws)
        await db.flush()
        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        return ws, cv


class _Recorder:
    """Stands in for the fanout and reads the row as a subscriber would.

    Substituted for `run_events._publish`, the one coroutine every deferred event
    goes through, rather than for a named publisher: the payload is the wire
    contract, so parsing it here is what a subscriber does.
    """

    def __init__(self) -> None:
        #: (old_status, new_status, what another session could read at that moment)
        self.seen: list[tuple[str, str, str | None]] = []

    async def __call__(self, channels: list[str], payload: str) -> None:
        body = json.loads(payload)
        if body.get("event") != "run_status_change":
            return
        async with get_db_session() as probe:
            row = (
                await probe.execute(select(Run).where(Run.id == _uuid.UUID(body["run_id"])))
            ).scalar_one_or_none()
            self.seen.append(
                (
                    body["old_status"],
                    body["new_status"],
                    row.status if row is not None else None,
                )
            )


class TestTheRowIsVisibleWhenTheEventFires:
    async def test_an_ordinary_transition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ws, cv = await _seed()
        rec = _Recorder()
        monkeypatch.setattr(run_events, "_publish", rec)

        async with get_db_session() as db:
            fresh = (await db.execute(select(Workspace).where(Workspace.id == ws.id))).scalar_one()
            run = await run_service.create_run(db, fresh, configuration_version_id=cv.id)
            run = await run_service.transition_run(db, run, "queued")
            run = await run_service.transition_run(db, run, "planning")
            await db.commit()
        await _settle()

        assert rec.seen, "no event was published at all"

        # THE defect, stated exactly: an event that says "we left `old_status`"
        # must never be published while another session can still read
        # `old_status`. That is what the browser saw -- it refetched on receipt
        # and got the status the run had just moved away from.
        stale = [(o, n, v) for o, n, v in rec.seen if v == o]
        assert stale == [], f"published while the row still read its old status: {stale}"

        # And the LAST event of a transaction describes the row exactly, because
        # by then nothing further has moved it.
        o, n, v = rec.seen[-1]
        assert v == n, f"the final event claimed {n!r} but the row read {v!r}"

    async def test_the_configure_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The reported case: `configuring -> applied`, the reconciler's only driver."""
        ws, cv = await _seed()
        async with get_db_session() as db:
            fresh = (await db.execute(select(Workspace).where(Workspace.id == ws.id))).scalar_one()
            run = await run_service.create_run(db, fresh, configuration_version_id=cv.id)
            for status in ("queued", "planning", "planned", "confirmed", "applying"):
                run = await run_service.transition_run(db, run, status)
            run_id = run.id
            await db.commit()
        await _settle()

        rec = _Recorder()
        monkeypatch.setattr(run_events, "_publish", rec)
        async with get_db_session() as db:
            live = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
            # `configuring` is reachable from `applying` -- the composite hand-over
            # -- which is the shape the reported bug was seen on.
            live = await run_service.transition_run(db, live, "configuring")
            live = await run_service.transition_run(db, live, "applied")
            await db.commit()
        await _settle()

        assert [n for _, n, _ in rec.seen] == ["configuring", "applied"]
        stale = [(o, n, v) for o, n, v in rec.seen if v == o]
        assert stale == [], f"published while the row still read its old status: {stale}"
        # The terminal specifically: this is the badge the report was about.
        assert rec.seen[-1] == ("configuring", "applied", "applied"), (
            f"the terminal event did not describe a visible row: {rec.seen[-1]}"
        )


class TestNothingIsAnnouncedThatDidNotHappen:
    async def test_a_rolled_back_transition_publishes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws, cv = await _seed()
        rec = _Recorder()
        monkeypatch.setattr(run_events, "_publish", rec)

        async with get_db_session() as db:
            fresh = (await db.execute(select(Workspace).where(Workspace.id == ws.id))).scalar_one()
            run = await run_service.create_run(db, fresh, configuration_version_id=cv.id)
            await run_service.transition_run(db, run, "queued")
            await db.rollback()
        await _settle()

        assert rec.seen == [], f"a rolled-back transition announced itself: {rec.seen}"


class TestTheNoOpGuardCannotBecomeTheProductionPath:
    async def test_a_real_session_info_is_a_dict(self) -> None:
        """`run_after_commit` no-ops on a non-dict `info`, which must mean a mock only.

        The guard exists because `AsyncMock().info.setdefault(...)` returns a
        coroutine. If a real session's `info` ever stopped being a dict, every
        deferred publish would silently become a no-op in production.
        """
        async with get_db_session() as db:
            assert isinstance(db.info, dict)
