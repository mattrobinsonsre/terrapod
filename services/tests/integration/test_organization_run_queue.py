"""The deployment's run queue and capacity, as SQL (#2378).

Integration tier, and necessarily so: all three properties under test are in the
WHERE and ORDER BY, and a services-tier test handing the function rows through a
mocked `db.execute` asserts what the loop does with rows it was given and can say
nothing about which rows it asks for.

What the queries have to get right:

  * **only waiting runs**, which is `queued` (a plan or a standalone configure),
    `confirmed` (an apply) and `pending` (a run whose configuration version is
    still uploading). A run a runner is already working is not waiting.
  * **oldest first**, which is the opposite of every other run listing here. The
    position IS the feature: `waitForRun` reads it off the caller's own run and
    subtracts capacity's `running` from it, so position 1 must be the run that
    has been waiting longest.
  * **Terraform only.** The queue document is serialised in the TFE run
    vocabulary, and a Pulumi or Ansible run's status is not a value `go-tfe` can
    switch on (#1905). A run with no engine of its own inherits its workspace's,
    which is why the filter is a subquery and why this needs a real database.
  * **capacity describes the same population**, because the CLI subtracts one
    number from the other.
"""

import uuid

import pytest

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import run_service

pytestmark = pytest.mark.integration


async def _seed(tag: str, rows: list[tuple[str, str, str]]) -> dict[str, uuid.UUID]:
    """Seed `(label, engine, status)` rows, one workspace per engine.

    Through the ORM, not hand-written SQL: `runs` is a wide table and an INSERT
    naming its columns breaks on the next NOT NULL anyone adds, with a driver
    error about a column that has nothing to do with the test.
    """
    ids: dict[str, uuid.UUID] = {}
    async with get_db_session() as db:
        workspaces: dict[str, uuid.UUID] = {}
        for label, engine, status in rows:
            if engine not in workspaces:
                ws = Workspace(name=f"orgq-{tag}-{engine}", engine=engine)
                db.add(ws)
                await db.flush()
                workspaces[engine] = ws.id
            run = Run(workspace_id=workspaces[engine], created_by="tests", status=status)
            db.add(run)
            await db.flush()
            ids[label] = run.id
    return ids


async def _queue() -> list[tuple[uuid.UUID, int]]:
    async with get_db_session() as db:
        return [
            (run.id, position) for run, position in await run_service.organization_run_queue(db)
        ]


async def _capacity() -> tuple[int, int]:
    async with get_db_session() as db:
        return await run_service.organization_run_capacity(db)


class TestWhichRunsAreInTheQueue:
    async def test_only_the_waiting_states_appear(self, _create_tables):
        ids = await _seed(
            "states",
            [
                ("pending", "terraform", "pending"),
                ("queued", "terraform", "queued"),
                ("confirmed", "terraform", "confirmed"),
                ("planning", "terraform", "planning"),
                ("applying", "terraform", "applying"),
                ("configuring", "terraform", "configuring"),
                ("planned", "terraform", "planned"),
                ("applied", "terraform", "applied"),
                ("errored", "terraform", "errored"),
            ],
        )
        queued = {run_id for run_id, _ in await _queue()}
        waiting = {ids["pending"], ids["queued"], ids["confirmed"]}
        assert waiting <= queued, "every waiting run must be listed"
        working = {ids[k] for k in ("planning", "applying", "configuring")}
        assert not (working & queued), "a run already being worked is not waiting"
        done = {ids[k] for k in ("planned", "applied", "errored")}
        assert not (done & queued)

    async def test_the_states_match_the_dispatcher(self, _create_tables):
        """`WAITING_STATES` and `EXECUTING_STATES` must agree with the statuses
        `claim_next_run` actually selects on, or the queue goes quietly short of
        the runs it exists to report. Derived from the claim loop's own tuples
        rather than restated, so a new phase added there fails here.
        """
        import ast
        import inspect

        src = inspect.getsource(run_service.claim_next_run)
        tree = ast.parse(inspect.cleandoc(src))
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and isinstance(node.iter, ast.List):
                for element in node.iter.elts:
                    if isinstance(element, ast.Tuple) and element.elts:
                        first = element.elts[0]
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            targets.add(first.value)
        assert targets, "could not read the claim loop's target statuses"
        # Every status the dispatcher claims a run OUT of is either waiting for a
        # runner or already being worked by one -- there is no third kind.
        unclassified = targets - run_service.WAITING_STATES - run_service.EXECUTING_STATES
        assert not unclassified, (
            f"`claim_next_run` claims runs out of {sorted(unclassified)}, which is in "
            "neither WAITING_STATES nor EXECUTING_STATES -- so the queue and capacity "
            "do not know about it"
        )
        assert not (run_service.WAITING_STATES & run_service.EXECUTING_STATES)

    async def test_a_pulumi_run_is_not_in_the_queue(self, _create_tables):
        """The engine filter, which is the half that needs a real database: a run
        carries no engine, so the filter is a subquery over its workspace."""
        ids = await _seed(
            "engine",
            [
                ("terraform", "terraform", "queued"),
                ("pulumi", "pulumi", "queued"),
            ],
        )
        queued = {run_id for run_id, _ in await _queue()}
        assert ids["terraform"] in queued
        assert ids["pulumi"] not in queued, (
            "a Pulumi run's status is not a value go-tfe can switch on, so it must "
            "not reach a document serialised in the TFE vocabulary (#1905)"
        )


class TestThePositions:
    async def test_oldest_first_and_one_based(self, _create_tables):
        ids = await _seed(
            "order",
            [
                ("first", "terraform", "queued"),
                ("second", "terraform", "queued"),
                ("third", "terraform", "queued"),
            ],
        )
        queue = await _queue()
        mine = [(rid, pos) for rid, pos in queue if rid in set(ids.values())]
        assert [pos for _, pos in mine] == sorted(pos for _, pos in mine), "ascending"
        order = [rid for rid, _ in mine]
        assert order == [ids["first"], ids["second"], ids["third"]], (
            "a queue is served in the order it was joined, so the run that has been "
            "waiting longest is first -- the opposite of every other run listing"
        )
        assert min(pos for _, pos in queue) == 1, "positions are 1-based"

    async def test_the_positions_are_contiguous(self, _create_tables):
        """`waitForRun` subtracts `running` from a position, so a gap would make
        the displayed "waiting for N runs" wrong by the size of the gap."""
        await _seed("contig", [(f"r{i}", "terraform", "queued") for i in range(4)])
        positions = [pos for _, pos in await _queue()]
        assert positions == list(range(1, len(positions) + 1))


class TestTheCapacity:
    async def test_it_counts_the_same_population_as_the_queue(self, _create_tables):
        await _seed(
            "cap",
            [
                ("w1", "terraform", "queued"),
                ("w2", "terraform", "confirmed"),
                ("w3", "terraform", "pending"),
                ("r1", "terraform", "planning"),
                ("r2", "terraform", "applying"),
                ("done", "terraform", "applied"),
            ],
        )
        pending, running = await _capacity()
        assert pending == len(await _queue()), (
            "`pending` and the queue are the same set, so the CLI's "
            "`position - running` describes one population"
        )
        assert pending >= 3 and running >= 2

    async def test_a_pulumi_run_is_in_neither_count(self, _create_tables):
        before_pending, before_running = await _capacity()
        await _seed(
            "capengine",
            [
                ("pq", "pulumi", "queued"),
                ("pa", "pulumi", "applying"),
            ],
        )
        after_pending, after_running = await _capacity()
        assert (after_pending, after_running) == (before_pending, before_running), (
            "capacity must describe the same engine-filtered population as the "
            "positions it is subtracted from"
        )

    async def test_a_canceling_run_counts_as_running(self, _create_tables):
        """Its Job is still winding down, so it still occupies a runner -- and it
        is reached only from the apply side, never from `planning`."""
        before = (await _capacity())[1]
        await _seed("cancel", [("c", "terraform", "canceling")])
        assert (await _capacity())[1] == before + 1
