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


async def _stamp(limit: str | None, *, document=None, narrowed_hosts=("web1",)):
    """Drive the stamp, returning (result, document, storage)."""
    doc = dict(WHOLE if document is None else document)
    storage = MagicMock()
    storage.put = AsyncMock()
    narrowed = {"hosts": {h: {} for h in narrowed_hosts}}
    with (
        patch.object(ir, "resolve_for_run", AsyncMock(return_value=narrowed)) as resolve,
        patch("terrapod.storage.get_storage", return_value=storage),
    ):
        out = await ir.stamp_targeted_hosts(AsyncMock(), _run(limit), doc, "the/key")
    return out, doc, storage, resolve


class TestNothingIsRecordedWhenThereIsNothingToRecord:
    @pytest.mark.asyncio
    async def test_a_run_with_no_limit_of_its_own(self) -> None:
        """The ordinary case. Such a run targets the artifact as stored, and
        `resolve_for_run` bounds EVERY resolution -- so even an unlimited write
        is already narrowed to the workspace's `hard_limit`."""
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
