"""Turning the event stream into rows (#1928).

What comes into Postgres is the per-host summary plus failed and unreachable
events, and nothing else -- measured volume is why: a four-host play with one
25-item loop produced 107 events and 79 KB, 75 of them loop items. Row-per-event
at that rate is a known scaling constraint for any event-per-row design.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.db.models import RunHostEvent, RunHostResult
from terrapod.services import host_results_service


def _run():
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    return run


def _stream(records: list[dict] | str):
    """A storage whose object yields these records, in two chunks.

    Two chunks deliberately, split mid-line: the reader assembles lines across
    reads, and a single-chunk fixture cannot tell a correct reader from one that
    assumes every read ends on a newline.
    """
    body = records if isinstance(records, str) else "".join(json.dumps(r) + "\n" for r in records)
    raw = body.encode()
    cut = max(1, len(raw) // 2)

    async def get_stream(_key):
        yield raw[:cut]
        yield raw[cut:]

    storage = MagicMock()
    storage.get_stream = get_stream
    return storage


def _db():
    db = MagicMock()
    db.add = MagicMock()
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=empty)
    return db


def _added(db, model):
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], model)]


async def _ingest(records):
    db, run = _db(), _run()
    with patch.object(host_results_service, "get_storage", return_value=_stream(records)):
        counts = await host_results_service.ingest(db, run)
    return db, run, counts


@pytest.mark.asyncio
class TestTheRecap:
    async def test_one_row_per_host_with_every_counter(self):
        db, _run_, (hosts, events) = await _ingest(
            [
                {
                    "kind": "stats",
                    "recap": {
                        "web1": {
                            "ok": 5,
                            "changed": 2,
                            "failures": 0,
                            "unreachable": 0,
                            "skipped": 1,
                            "rescued": 0,
                            "ignored": 0,
                        }
                    },
                }
            ]
        )
        assert (hosts, events) == (1, 0)
        row = _added(db, RunHostResult)[0]
        assert (row.host, row.ok, row.changed, row.skipped) == ("web1", 5, 2, 1)

    async def test_saw_unreachable_comes_from_the_event_not_the_counters(self):
        """The measured disagreement, and the whole reason the column exists.

        With `ignore_unreachable: true` ansible reports `unreachable=0
        ignored=1` while the unreachable hook fires. Deriving this from the
        counters calls a dead host a healthy one.
        """
        db, _run_, _ = await _ingest(
            [
                {"kind": "unreachable", "host": "hdead", "task": "ping", "seq": 1},
                {
                    "kind": "stats",
                    "recap": {"hdead": {"ok": 2, "unreachable": 0, "ignored": 1}},
                },
            ]
        )
        row = next(r for r in _added(db, RunHostResult) if r.host == "hdead")
        assert row.unreachable == 0, "the fixture's premise: the counter says zero"
        assert row.saw_unreachable is True

    async def test_a_host_whose_only_trace_is_a_failure_still_gets_a_row(self):
        """A Job killed mid-play writes events and no recap.

        Without this the host that failed is the one that vanishes.
        """
        db, _run_, (hosts, events) = await _ingest(
            [{"kind": "failed", "host": "web2", "task": "t", "seq": 4}]
        )
        assert (hosts, events) == (1, 1)
        assert [r.host for r in _added(db, RunHostResult)] == ["web2"]


@pytest.mark.asyncio
class TestWhichEventsBecomeRows:
    async def test_only_failed_and_unreachable(self):
        db, _run_, (_hosts, events) = await _ingest(
            [
                {"kind": "ok", "host": "web1", "task": "t", "seq": 1},
                {"kind": "item_ok", "host": "web1", "task": "t", "seq": 2},
                {"kind": "skipped", "host": "web1", "task": "t", "seq": 3},
                {"kind": "task_start", "task": "t", "seq": 4},
                {"kind": "failed", "host": "web2", "task": "boom", "seq": 5},
                {"kind": "unreachable", "host": "hdead", "task": "ping", "seq": 6},
            ]
        )
        assert events == 2
        assert {r.kind for r in _added(db, RunHostEvent)} == {"failed", "unreachable"}

    async def test_the_message_is_taken_from_an_inline_payload(self):
        db, _run_, _ = await _ingest(
            [
                {
                    "kind": "failed",
                    "host": "web2",
                    "task": "boom",
                    "seq": 1,
                    "payload": {"msg": "non-zero return code"},
                }
            ]
        )
        assert _added(db, RunHostEvent)[0].msg == "non-zero return code"

    async def test_a_spilled_payload_keeps_its_reference_and_its_message(self):
        db, _run_, _ = await _ingest(
            [
                {
                    "kind": "failed",
                    "host": "web2",
                    "task": "boom",
                    "seq": 1,
                    "payload_ref": 7,
                    "msg": "truncated for the row",
                }
            ]
        )
        row = _added(db, RunHostEvent)[0]
        assert (row.payload_ref, row.msg) == (7, "truncated for the row")

    async def test_the_task_uuid_is_stored_but_the_name_is_what_is_indexed(self):
        """Measured: `task._uuid` differs between runs of the same playbook.

        So it correlates within one run and nothing across runs, which is why
        the cross-run index is on `task_name`.
        """
        db, _run_, _ = await _ingest(
            [
                {
                    "kind": "failed",
                    "host": "web2",
                    "task": "restart nginx",
                    "action": "ansible.builtin.service",
                    "task_uuid": "16b865bb-48aa-049a-9886-000000000007",
                    "seq": 1,
                }
            ]
        )
        row = _added(db, RunHostEvent)[0]
        assert row.task_name == "restart nginx"
        assert row.task_action == "ansible.builtin.service"
        assert row.task_uuid.endswith("000000000007")
        # The index is on the NAME, which is the only one stable across runs.
        indexed = {tuple(index.columns.keys()) for index in RunHostEvent.__table__.indexes}
        assert ("workspace_id", "task_name") in indexed, indexed
        assert not any("task_uuid" in columns for columns in indexed), (
            "an index on task_uuid would imply it is a cross-run key, and it is not"
        )


@pytest.mark.asyncio
class TestItToleratesAStreamItCannotFullyRead:
    async def test_a_truncated_final_line_is_skipped_not_fatal(self):
        """A Job killed mid-write leaves exactly this.

        Refusing the whole upload would discard every event that did arrive --
        which are the ones somebody needs when a configure was killed.
        """
        body = (
            json.dumps({"kind": "failed", "host": "web2", "task": "t", "seq": 1})
            + "\n"
            + '{"kind": "failed", "host": "we'
        )
        _, _, (hosts, events) = await _ingest(body)
        assert (hosts, events) == (1, 1)

    async def test_a_non_object_line_is_skipped(self):
        body = (
            '["not an object"]\n'
            + json.dumps({"kind": "failed", "host": "web2", "task": "t", "seq": 1})
            + "\n"
        )
        _, _, (_hosts, events) = await _ingest(body)
        assert events == 1

    async def test_an_empty_stream_yields_nothing_rather_than_failing(self):
        db, _run_, counts = await _ingest("")
        assert counts == (0, 0)
        assert db.add.call_count == 0

    async def test_the_event_rows_are_capped_while_the_stream_keeps_everything(self):
        over = host_results_service.MAX_EVENT_ROWS + 25
        records = [{"kind": "failed", "host": f"h{i}", "task": "t", "seq": i} for i in range(over)]
        _, _, (_hosts, events) = await _ingest(records)
        assert events == host_results_service.MAX_EVENT_ROWS


@pytest.mark.asyncio
class TestItIsIdempotentByReplacement:
    async def test_a_re_upload_deletes_what_was_there_first(self):
        """Otherwise a second ingest doubles every event.

        The stream is append-only within one run, so replacement is the correct
        shape rather than a merge.
        """
        db, run = _db(), _run()
        stale_result, stale_event = RunHostResult(host="old"), RunHostEvent(host="old")
        existing = MagicMock()
        existing.scalars.return_value.all.side_effect = [[stale_result], [stale_event]]
        db.execute = AsyncMock(return_value=existing)
        with patch.object(host_results_service, "get_storage", return_value=_stream([])):
            await host_results_service.ingest(db, run)
        deleted = [c.args[0] for c in db.delete.call_args_list]
        assert stale_result in deleted and stale_event in deleted


def _capturing_stream(records):
    """Like `_stream`, but also records every `put` so partitions are visible."""
    storage = _stream(records)
    puts: dict[str, bytes] = {}

    async def put(key, body, content_type="application/octet-stream", **_):
        puts[key] = body
        return MagicMock()

    storage.put = put
    return storage, puts


async def _ingest_capturing(records):
    db, run = _db(), _run()
    storage, puts = _capturing_stream(records)
    with patch.object(host_results_service, "get_storage", return_value=storage):
        await host_results_service.ingest(db, run)
    return db, run, puts


def _partition(puts, run, host):
    from terrapod.storage.keys import host_events_host_key

    return puts[host_events_host_key(str(run.workspace_id), str(run.id), host)]


@pytest.mark.asyncio
class TestThePerHostPartition:
    """#1928: "events are ALSO written partitioned per host".

    The point is that a drill-down is one object fetch rather than a scan of the
    whole run's stream, so what matters is that each object holds EVERYTHING
    that happened to its host -- not the failures, which are already rows.
    """

    RECORDS = [
        {"kind": "playbook_start", "seq": 1},
        {"kind": "task_start", "task_name": "ping", "seq": 2},
        {"kind": "ok", "host": "web1", "task_name": "ping", "seq": 3},
        {"kind": "ok", "host": "web2", "task_name": "ping", "seq": 4},
        {"kind": "failed", "host": "web2", "task_name": "deploy", "msg": "boom", "seq": 5},
        {"kind": "stats", "recap": {"web1": {"ok": 1}, "web2": {"ok": 1, "failures": 1}}},
    ]

    async def test_one_object_per_host_naming_only_that_host(self):
        _, run, puts = await _ingest_capturing(self.RECORDS)
        assert len(puts) == 2, f"expected one object per host, got {list(puts)}"
        web1 = _partition(puts, run, "web1").decode()
        web2 = _partition(puts, run, "web2").decode()
        assert '"web1"' in web1 and '"web2"' not in web1
        assert '"web2"' in web2 and '"web1"' not in web2

    async def test_interleaved_hosts_each_get_a_complete_in_order_partition(self):
        """Each write is its own append, so interleaving must not lose or reorder.

        This used to patch `MAX_OPEN_PARTITION_FILES` down to 2 and assert the
        LRU never held more than that, because a reopen after eviction was
        where a lost or reordered append would have come from. There is no
        cache to bound now -- `add` appends inside its own `with` -- so the
        bound has nothing left to assert, while the property it was protecting
        is unchanged and is what this checks.
        """
        records = [
            {"kind": "ok", "host": host, "seq": seq}
            for seq, host in enumerate(("web1", "web2", "web3", "web1", "web2", "web3"))
        ]
        _, run, puts = await _ingest_capturing(records)

        assert len(puts) == 3
        for host in ("web1", "web2", "web3"):
            expected = "".join(json.dumps(r) + "\n" for r in records if r["host"] == host)
            assert _partition(puts, run, host) == expected.encode()

    async def test_the_bookkeeping_grows_with_hosts_not_with_writes(self):
        """One entry per HOST, however many times that host is written.

        Its predecessor measured an `ExitStack`'s private callback deque,
        because the release mechanism was a per-host callback and registering
        one per `open` made it grow with the number of WRITES -- ~10,000 for a
        500-host play with 20 tasks (#2425). That mechanism went with the handle
        cache, and the memory property it was protecting now belongs to the path
        map: this class exists so the API pod's memory does not scale with the
        operator's estate, and it must not scale with the stream's length
        either.
        """
        depths = []
        real = host_results_service._Partitioner

        class _Spy(real):
            def add(self, host, raw):
                super().add(host, raw)
                depths.append(len(self._paths))

        hosts = ("web1", "web2", "web3")
        records = [{"kind": "ok", "host": host, "seq": seq} for seq, host in enumerate(hosts * 4)]
        with patch.object(host_results_service, "_Partitioner", _Spy):
            _, run, puts = await _ingest_capturing(records)

        assert len(records) == 12, "the point is that writes outnumber hosts"
        assert max(depths) == len(hosts), (
            f"the map reached {max(depths)} entries for {len(hosts)} hosts "
            f"over {len(records)} writes -- it is growing per write"
        )
        # And every partition is still complete, so the bound was not bought
        # by dropping a reopen.
        assert len(puts) == len(hosts)

    async def test_the_partition_is_NOT_only_the_failures(self):
        """The trap. Rows hold `failed`/`unreachable`; the partition holds all.

        Putting the capture after the kind filter would make each partition a
        copy of what Postgres already has, which is the one shape that reads as
        working and delivers nothing.
        """
        _, run, puts = await _ingest_capturing(self.RECORDS)
        web2 = _partition(puts, run, "web2").decode()
        assert '"ok"' in web2, "the ok event is missing — captured after the kind filter?"
        assert '"failed"' in web2

    async def test_a_line_naming_no_host_lands_in_no_partition(self):
        """`playbook_start`, `task_start` and `stats` belong to the run."""
        _, run, puts = await _ingest_capturing(self.RECORDS)
        for body in puts.values():
            text = body.decode()
            assert "playbook_start" not in text
            assert "task_start" not in text
            assert '"stats"' not in text

    async def test_each_line_is_byte_exact_and_newline_terminated(self):
        """A partition is a subsequence of the stream, not a re-serialisation.

        Re-encoding the parsed record would normalise key order and spacing, so
        the whole-run object and the partition would disagree byte for byte and
        a reader diffing them would see changes that are not there.

        **The body here is hand-written with NON-CANONICAL spacing on purpose.**
        A fixture built with `json.dumps` cannot test this at all: the
        re-serialisation of its own parsed record is identical to the line it
        came from, so the assertion holds either way. Measured -- the mutation
        that swaps `raw` for `json.dumps(record)` SURVIVED the first version of
        this test, which is the whole reason the fixture is a string.
        """
        body = (
            '{"kind":"playbook_start","seq":1}\n'
            '{ "kind" : "ok" ,  "host" : "web1" ,  "seq" : 3 }\n'
            '{"kind":"stats","recap":{"web1":{"ok":1}}}\n'
        )
        _, run, puts = await _ingest_capturing(body)
        web1 = _partition(puts, run, "web1")
        assert web1 == b'{ "kind" : "ok" ,  "host" : "web1" ,  "seq" : 3 }\n', (
            "the partition was re-serialised rather than copied verbatim"
        )

    async def test_an_upload_failure_does_not_lose_the_rows(self):
        """A partition is a read-side convenience, so it must not be fatal.

        The whole-run stream and every row are already stored; failing the
        ingest over a partition would lose those too.
        """
        db, run = _db(), _run()
        storage = _stream(self.RECORDS)

        async def boom(*_a, **_k):
            raise RuntimeError("bucket on fire")

        storage.put = boom
        with patch.object(host_results_service, "get_storage", return_value=storage):
            hosts, events = await host_results_service.ingest(db, run)
        assert hosts == 2
        assert len(_added(db, RunHostResult)) == 2

    async def test_the_temp_directory_is_removed_even_when_the_upload_fails(self):
        """Else a failed ingest leaves a copy of the whole stream on the PVC.

        Which is the failure the disk buffering exists to avoid, so the cleanup
        is in a `finally` and this is what pins it there.
        """
        import tempfile

        made: list[str] = []
        real = tempfile.mkdtemp

        def record(*a, **k):
            d = real(*a, **k)
            made.append(d)
            return d

        db, run = _db(), _run()
        storage = _stream(self.RECORDS)

        async def boom(*_a, **_k):
            raise RuntimeError("nope")

        storage.put = boom
        with (
            patch.object(host_results_service, "get_storage", return_value=storage),
            patch.object(tempfile, "mkdtemp", record),
        ):
            await host_results_service.ingest(db, run)

        assert made, "the partitioner did not create a temp directory"
        import os

        for d in made:
            assert not os.path.isdir(d), f"{d} was left behind"


@pytest.mark.asyncio
class TestTheSpoolDirectoryIsAlwaysRemoved:
    """The partitioner's lifetime is a `with`, not a `try` around `flush` alone.

    It writes a full copy of the stream split by host into a temp directory, so
    a cleanup that guarded only `flush` left that directory behind whenever
    anything between the two raised -- a storage error mid-stream, or a failing
    `db.flush`. That is exactly the PVC-filling failure the cleanup exists to
    prevent, and what CodeQL's py/file-not-closed first pointed at.

    There are no open handles left to assert about, because each append is its
    own `with`. That is the stronger property, and a behavioural test cannot
    see a cached handle reintroduced later -- so it is pinned over the source
    by `test_every_open_in_this_module_is_a_with` below.
    """

    async def test_a_failure_after_the_stream_still_removes_the_directory(self):
        created: list = []
        real = host_results_service._Partitioner

        class _Spy(real):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                created.append(self)

        db, run = _db(), _run()
        db.flush = AsyncMock(side_effect=RuntimeError("the database went away"))
        records = [
            {"kind": "failed", "host": "web1", "task": "t", "seq": 1},
            {"kind": "failed", "host": "web2", "task": "t", "seq": 2},
        ]
        with (
            patch.object(host_results_service, "get_storage", return_value=_stream(records)),
            patch.object(host_results_service, "_Partitioner", _Spy),
        ):
            with pytest.raises(RuntimeError):
                await host_results_service.ingest(db, run)

        assert created, "the partitioner was never constructed"
        partitioner = created[0]
        # Without this the assertion below passes vacuously: nothing needs
        # removing if nothing was ever spooled.
        assert len(partitioner._paths) == 2, partitioner._paths
        assert not os.path.exists(partitioner._dir)


def test_every_open_in_this_module_is_a_with() -> None:
    """No `open` in `host_results_service` may outlive its own statement.

    This is the durable half of the fix for CodeQL's py/file-not-closed, which
    was raised against this module three times and dismissed twice -- alert 550,
    then 558 twelve lines further down, then 566 once the file moved again. A
    dismissal is per-alert, so it does not survive the file being edited, which
    makes it a deferral rather than a disposition. The construct the query
    objects to is therefore gone instead of argued with, and this is what keeps
    it gone: a cached handle cannot come back without failing here, where the
    behavioural test above would not notice it.

    Derived from the module's own source rather than from a list of call sites,
    so a new `open` anywhere in it is in scope automatically, and floored so a
    matcher that stops matching fails instead of passing on nothing.
    """
    tree = ast.parse(pathlib.Path(host_results_service.__file__).read_text())

    guarded = {
        item.context_expr
        for node in ast.walk(tree)
        if isinstance(node, ast.With | ast.AsyncWith)
        for item in node.items
    }
    opens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"
    ]

    assert len(opens) >= 2, (
        f"the matcher found {len(opens)} open() calls in host_results_service; it is "
        "meant to see every one, so a floor this low failing means the matcher broke"
    )
    loose = sorted(node.lineno for node in opens if node not in guarded)
    assert loose == [], (
        "every open() in host_results_service must be the context expression of a "
        "`with`, so that its close is checkable by reading one line rather than by "
        f"tracing a handle through a dict. Loose open() at line(s): {loose}"
    )
