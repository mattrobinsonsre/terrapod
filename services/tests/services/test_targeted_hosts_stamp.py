"""Recording what a run's own `--limit` selects, on its artifact (#2690).

The artifact itself is deliberately WHOLE: `--limit` does not restrict
`delegate_to`, so narrowing the inventory a play is handed would break a
playbook that delegates to a host outside the limit, which `--limit` permits.
Applying the limit is the runner's job. This records what it will select, so the
API can answer "which hosts did this run target" without guessing -- and
without that record every consumer of that question was handed the entire
inventory.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import inventory_resolve as ir
from tests.post_plan_stubs import stub_run_lifecycle_task_stages


def _run(limit: str | None):
    return MagicMock(id=uuid.uuid4(), workspace_id=uuid.uuid4(), ansible_limit=limit)


WHOLE = {"hosts": {"web1": {}, "web2": {}, "db1": {}}, "groups": {"web": ["web1", "web2"]}}


async def _stamp(
    limit: str | None,
    *,
    document=None,
    narrowed_hosts=("web1",),
    default_limit: str = "",
    hard_limit: str = "",
    workspace_missing: bool = False,
):
    """Drive the stamp, returning (result, document, storage, resolve).

    The workspace is PASSED to the stamp, with both limits defaulting to empty,
    because the stamp reads the run's EFFECTIVE limit -- the run's own, falling
    back to the workspace's `default_limit`, bounded by `hard_limit`, exactly as
    the engine builds the Job's.

    `ws=None` drives the fail-wide branch. It used to be reachable only by
    arranging for `db.get` to return None; now it is an argument, which is the
    point of the workspace being passed rather than fetched -- a caller's mock db
    returning a truthy `AsyncMock` workspace was how four unrelated tests in
    `test_configure_ee_image` came to fail on an unrelated change.
    """
    doc = dict(WHOLE if document is None else document)
    storage = MagicMock()
    storage.put = AsyncMock()
    narrowed = {"hosts": {h: {} for h in narrowed_hosts}}
    ws = (
        None if workspace_missing else MagicMock(default_limit=default_limit, hard_limit=hard_limit)
    )
    db = AsyncMock()
    with (
        patch.object(ir, "resolve_for_run", AsyncMock(return_value=narrowed)) as resolve,
        patch("terrapod.storage.get_storage", return_value=storage),
    ):
        out = await ir.stamp_targeted_hosts(db, _run(limit), doc, "the/key", ws)
    return out, doc, storage, resolve


class TestNothingIsRecordedWhenThereIsNothingToRecord:
    @pytest.mark.asyncio
    async def test_a_run_with_no_limit_and_no_workspace_default(self) -> None:
        """The ordinary case: NOTHING narrows, so the artifact as stored IS the
        target set and there is nothing to record.

        This test used to say "a run with no limit of its own", which was the
        false claim -- a workspace with `default_limit` set narrows such a run
        at the runner while the run's own column stays empty. Both halves have
        to be empty, which is what `effective_limit` answers.
        """
        out, doc, storage, resolve = await _stamp(None)
        assert out is None
        assert ir.TARGETED not in doc
        storage.put.assert_not_awaited()
        resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_whitespace_only_limit_is_no_limit(self) -> None:
        out, doc, storage, _ = await _stamp("   ")
        assert out is None and ir.TARGETED not in doc
        storage.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_inventory(self) -> None:
        """A limit over no hosts selects no hosts, which reading the artifact
        already answers. An empty inventory is a legitimate resolution --
        `write_run_artifact` stores one rather than skipping it."""
        out, doc, storage, resolve = await _stamp("web1", document={"hosts": {}})
        assert out is None
        storage.put.assert_not_awaited()
        resolve.assert_not_awaited()


class TestTheRecord:
    @pytest.mark.asyncio
    async def test_it_records_the_hosts_the_limit_selects(self) -> None:
        out, doc, storage, resolve = await _stamp("web1", narrowed_hosts=("web1",))
        assert out == ["web1"]
        assert doc[ir.TARGETED] == {"limit": "web1", "hosts": ["web1"]}
        storage.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_ARTIFACT_ITSELF_stays_whole(self) -> None:
        """The whole point, and the thing a careless fix gets wrong: narrowing
        the stored inventory would break `delegate_to` to an excluded host."""
        _out, doc, storage, _ = await _stamp("web1", narrowed_hosts=("web1",))
        assert set(doc["hosts"]) == {"web1", "web2", "db1"}
        assert doc["groups"] == {"web": ["web1", "web2"]}
        stored = json.loads(storage.put.await_args.args[1].decode())
        assert set(stored["hosts"]) == {"web1", "web2", "db1"}, "the stored copy too"

    @pytest.mark.asyncio
    async def test_it_resolves_through_ansible_with_the_RUNS_OWN_limit(self) -> None:
        """Not matched here. A limit pattern carries globs, group names, `&`,
        `!` and `~regex`; a second implementation would be wrong in ways only a
        real inventory would reveal.

        The RAW limit is passed, not a pre-bounded one: `resolve_for_run` bounds
        every resolution itself, so bounding first would append the ceiling's
        terms twice.
        """
        _out, _doc, _storage, resolve = await _stamp("web:!db1")
        assert resolve.await_args.kwargs["limit"] == "web:!db1"

    @pytest.mark.asyncio
    async def test_a_limit_that_selects_NOTHING_is_recorded_as_nothing(self) -> None:
        """An empty selection is a real answer -- the run will configure no
        hosts -- and must be distinguishable from "no limit was set"."""
        out, doc, _storage, _ = await _stamp("ghost", narrowed_hosts=())
        assert out == []
        assert doc[ir.TARGETED] == {"limit": "ghost", "hosts": []}

    @pytest.mark.asyncio
    async def test_the_hosts_are_sorted(self) -> None:
        """So two reads of the same run produce the same record, and the
        `--limit` a retry derives from it is stable."""
        out, _doc, _s, _r = await _stamp("web", narrowed_hosts=("web2", "db1", "web1"))
        assert out == ["db1", "web1", "web2"]


class TestTheWorkspaceDefaultNarrowsToo:
    """#2690's second class of workspace, which the first fix did not reach.

    `limit_for` returns `""` whenever a derived configure adds no narrowing of
    its own, and its docstring says `build_job_spec` falls back to
    `default-limit`. So for a workspace with `default_limit` set, the RUNNER
    narrows and `run.ansible_limit` is empty -- and a stamp gated on the column
    alone never fires. The consumer then has neither a stamp nor a run limit and
    returns the whole inventory, which is verbatim the three consequences #2690
    exists to fix: a host filter offering hosts that cannot appear in the log,
    every excluded host reported `not-reached` (a status that ASSERTS the run
    targeted it), and an outstanding-only retry reaching hosts the operator
    excluded.
    """

    @pytest.mark.asyncio
    async def test_a_workspace_default_is_recorded(self) -> None:
        out, doc, storage, resolve = await _stamp(None, default_limit="web")
        assert out == ["web1"], "the workspace's default_limit did not reach the stamp"
        assert doc[ir.TARGETED]["limit"] == "web"
        assert doc[ir.TARGETED]["hosts"] == ["web1"]
        storage.put.assert_awaited_once()
        assert resolve.await_args.kwargs["limit"] == "web"

    @pytest.mark.asyncio
    async def test_the_runs_own_limit_wins_over_the_default(self) -> None:
        """Same precedence as the engine's: the run's own choice first."""
        out, doc, _, resolve = await _stamp("db1", default_limit="web")
        assert doc[ir.TARGETED]["limit"] == "db1"
        assert resolve.await_args.kwargs["limit"] == "db1"
        assert out is not None

    @pytest.mark.asyncio
    async def test_the_hard_limit_still_bounds_the_default(self) -> None:
        """`bounded_limit` intersects, so a hard limit narrows the default too."""
        _, doc, _, resolve = await _stamp(None, default_limit="web", hard_limit="prod")
        sent = resolve.await_args.kwargs["limit"]
        assert "prod" in sent and "web" in sent, f"hard limit not intersected: {sent!r}"

    @pytest.mark.asyncio
    async def test_an_unreadable_workspace_fails_wide(self) -> None:
        """A missing workspace row must not make a run look NARROWER than it was.

        A short target set under-reports what is outstanding, and a retry built
        from it would miss hosts -- so the degradation is to the run's own limit
        (here none, so no stamp) rather than to a guess.
        """
        out, doc, storage, resolve = await _stamp(None, workspace_missing=True)
        assert out is None
        resolve.assert_not_awaited()
        storage.put.assert_not_awaited()
        assert ir.TARGETED not in doc

        # And the degradation is to the run's OWN limit, not to nothing: a run
        # that did carry one still records it with no workspace to bound it.
        out2, doc2, _, _ = await _stamp("web1", workspace_missing=True)
        assert out2 == ["web1"]
        assert doc2[ir.TARGETED]["limit"] == "web1"


@pytest.mark.asyncio
class TestTheCallSitesPassTheirOwnWorkspace:
    """The stamp is handed the workspace; the two callers must hand it the real one.

    Every test above drives `stamp_targeted_hosts` directly, so all of them pass
    with a caller supplying `None` -- and `None` is the fail-WIDE branch, which
    restores #2690 exactly: the run's own column alone, so a `default_limit`
    workspace records no target set and every consumer is handed the whole
    inventory again. Measured: mutating `create_run`'s argument to `None` left
    twenty tests green.

    So these drive the CALLERS. A unit test of a function that gained a parameter
    says nothing about whether the call site passes the right thing.
    """

    @pytest.fixture(autouse=True)
    def _stub_post_plan_task_stages(self):
        """`create_run` reserves a post-plan task stage now that
        `runs.tfe_post_plan_decisions` defaults on (#2425), and that queries the
        db. This test drives the lifecycle against a narrow mock and has no
        opinion about stages -- the shared stub, not a patch of my own, so it
        cannot drift from what the lifecycle actually calls."""
        with stub_run_lifecycle_task_stages():
            yield

    async def test_create_run_passes_the_workspace_it_was_given(self) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        from terrapod.services.run_service import create_run

        # Every attribute `create_run` gates a configure on, set explicitly:
        # left as MagicMock attributes they are TRUTHY, so `locked` alone refuses
        # the run (#1975) and `ansible_execution_environment` resolves to an
        # unknown environment. The sibling fixture in `test_configure_ee_image`
        # records the same list for the same reason.
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.name = "stamp-ws"
        ws.engine = "terraform"
        ws.engine_version = "1.11"
        ws.execution_mode = "agent"
        ws.auto_apply = False
        ws.terragrunt_enabled = False
        ws.resource_cpu = "1"
        ws.resource_memory = "2Gi"
        ws.ee_resource_cpu = ""
        ws.ee_resource_memory = ""
        ws.parallelism = 10
        ws.configure_parallelism = None
        ws.agent_pool_links = []
        ws.plan_apply_enabled = True
        ws.configure_enabled = True
        ws.configure_after_apply = False
        ws.auto_configure_enabled = False
        ws.ansible_version = ""
        ws.ansible_execution_environment = ""
        ws.locked = False
        ws.config_directory = "ansible"
        # The point of the test: a workspace whose narrowing lives HERE and
        # nowhere on the run.
        ws.default_limit = "web"
        ws.hard_limit = ""
        db = AsyncMock(spec=AsyncSession)
        stamp = AsyncMock(return_value=None)
        with (
            patch("terrapod.services.run_service.Run") as MockRun,
            patch.object(ir, "write_run_artifact", AsyncMock(return_value=("k", dict(WHOLE)))),
            patch.object(ir, "stamp_targeted_hosts", stamp),
            # The derived-configure narrowing that runs just above the stamp. Not
            # the subject here -- it has its own tests -- and it queries the db,
            # so leaving it live means scripting a query to assert an argument.
            patch(
                "terrapod.services.inventory_narrowing.limit_for",
                AsyncMock(return_value=""),
            ),
        ):
            inst = MockRun.return_value
            inst.id = uuid.uuid4()
            inst.status = "pending"
            inst.workspace_id = ws.id
            inst.ansible_limit = ""
            await create_run(db, ws, message="m", is_configure=True)

        stamp.assert_awaited_once()
        # The WORKSPACE, by identity -- not None, and not a row re-read from the
        # db, which could be a different one than the caller validated.
        assert stamp.await_args[0][4] is ws


class TestNoCallSitePassesNoWorkspace:
    """DERIVED from the source, so a THIRD call site is covered the day it lands.

    The behavioural test above drives `create_run` only. `complete_apply` stamps
    too, and mutating ITS argument to `None` left twenty-one tests green --
    driving that function would mean scripting its whole apply path to assert one
    argument, so the honest tool is the source.

    `None` is the fail-WIDE branch: it means "there is no workspace row to bound
    this run", which for a caller that HAS one restores #2690 exactly. A caller
    may only pass a name it resolved.
    """

    def test_every_stamp_call_passes_a_resolved_workspace(self) -> None:
        import ast
        import pathlib

        from terrapod.services import run_service

        source = pathlib.Path(run_service.__file__).read_text()
        calls = [
            n
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "stamp_targeted_hosts"
        ]
        # A floor, so a renamed helper fails here rather than passing on nothing.
        assert len(calls) >= 2, f"found only {len(calls)} stamp call sites; the matcher has drifted"

        for call in calls:
            assert len(call.args) == 5, (
                f"line {call.lineno}: the workspace is positional argument 5; "
                "a keyword or a short call means this gate cannot read it"
            )
            ws_arg = call.args[4]
            assert not (isinstance(ws_arg, ast.Constant) and ws_arg.value is None), (
                f"line {call.lineno}: passes None as the workspace. That is the "
                "fail-wide branch, so a workspace's `default_limit` is never read "
                "and the run records no target set -- #2690, restored."
            )
            assert isinstance(ws_arg, ast.Name), (
                f"line {call.lineno}: expected a resolved name, got {type(ws_arg).__name__}"
            )
