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
):
    """Drive the stamp, returning (result, document, storage, resolve).

    The workspace is supplied EXPLICITLY, with both limits defaulting to empty,
    because the stamp reads the run's EFFECTIVE limit -- the run's own, falling
    back to the workspace's `default_limit`, bounded by `hard_limit`, exactly as
    the engine builds the Job's. A bare `AsyncMock()` db would hand it a mock
    whose `default_limit` is truthy, so every one of these tests would take the
    stamping branch for the wrong reason.
    """
    doc = dict(WHOLE if document is None else document)
    storage = MagicMock()
    storage.put = AsyncMock()
    narrowed = {"hosts": {h: {} for h in narrowed_hosts}}
    ws = MagicMock(default_limit=default_limit, hard_limit=hard_limit)
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    with (
        patch.object(ir, "resolve_for_run", AsyncMock(return_value=narrowed)) as resolve,
        patch("terrapod.storage.get_storage", return_value=storage),
    ):
        out = await ir.stamp_targeted_hosts(db, _run(limit), doc, "the/key")
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
        doc = dict(WHOLE)
        storage = MagicMock()
        storage.put = AsyncMock()
        db = AsyncMock()
        db.get = AsyncMock(return_value=None)
        with (
            patch.object(ir, "resolve_for_run", AsyncMock()) as resolve,
            patch("terrapod.storage.get_storage", return_value=storage),
        ):
            out = await ir.stamp_targeted_hosts(db, _run(None), doc, "the/key")
        assert out is None
        resolve.assert_not_awaited()
