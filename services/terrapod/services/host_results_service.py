"""Ingest and read a configure's per-host results (#1928).

The callback plugin writes one JSON object per line on the runner's shared
volume; the runner uploads that stream; this turns the two kinds that belong in
Postgres into rows and leaves everything else in the object.

**What comes into Postgres, and why only that.** Measured on ansible-core
2.18.3: a four-host play with one 25-item loop produced 107 events and 79 KB of
result payloads, 75 of them loop items, with a single `gather_facts` result at
16.5 KB per host. Row-per-event at that rate is a known scaling constraint for
any event-per-row design, so the rows are the per-host summary -- one per
`(run, host)`, bounded by the inventory -- plus failed and unreachable events,
which are normally zero. Those two buy what an object cannot: a run's failures
readable without a fetch, and "has this task ever failed, anywhere" as a query.

**Parsing is incremental, never buffered.** The route streams the body to
storage without holding it (rule 14) and this reads it back a chunk at a time,
parsing one line per step rather than one document (rule 13): a fleet-sized
stream is megabytes, and `json.loads` over the whole of it is a single sync block
on the event loop.

**A malformed line is skipped, not fatal.** The stream is evidence produced by a
plugin running in an operator's execution environment; a truncated final line
from a Job that was killed mid-write is the expected shape, and refusing the
whole upload would discard every event that did arrive -- which are the ones
somebody needs when a configure was killed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import Run, RunHostEvent, RunHostResult
from terrapod.logging_config import get_logger
from terrapod.storage import get_storage
from terrapod.storage.keys import host_events_host_key, host_events_key

logger = get_logger(__name__)

#: The only event kinds that become rows. Everything else stays in the object.
ROW_KINDS = ("failed", "unreachable")

#: A bound on rows from one run, so a playbook failing every task on every host
#: of a large fleet cannot write unboundedly. The stream keeps everything; this
#: caps only what Postgres holds, and the cap being hit is logged rather than
#: silent.
MAX_EVENT_ROWS = 2000

#: `msg` is for a reader to recognise the failure, not to hold the payload --
#: that stays in the stream, or in the sidecar when it is large.
_MSG_LIMIT = 4000


def _recap_row(run: Run, host: str, summary: dict[str, Any]) -> RunHostResult:
    return RunHostResult(
        run_id=run.id,
        workspace_id=run.workspace_id,
        host=host,
        ok=int(summary.get("ok") or 0),
        changed=int(summary.get("changed") or 0),
        failures=int(summary.get("failures") or 0),
        unreachable=int(summary.get("unreachable") or 0),
        skipped=int(summary.get("skipped") or 0),
        rescued=int(summary.get("rescued") or 0),
        ignored=int(summary.get("ignored") or 0),
    )


def _event_row(run: Run, record: dict[str, Any]) -> RunHostEvent:
    msg = record.get("msg")
    if not isinstance(msg, str):
        payload = record.get("payload")
        msg = payload.get("msg") if isinstance(payload, dict) else ""
    ref = record.get("payload_ref")
    return RunHostEvent(
        run_id=run.id,
        workspace_id=run.workspace_id,
        host=str(record.get("host") or ""),
        kind=str(record.get("kind") or ""),
        task_name=str(record.get("task") or "")[:500],
        task_action=str(record.get("action") or "")[:255],
        task_uuid=str(record.get("task_uuid") or "")[:64],
        play_name=str(record.get("play") or "")[:500],
        ignored=bool(record.get("ignored")),
        msg=(msg if isinstance(msg, str) else "")[:_MSG_LIMIT],
        payload_ref=int(ref) if isinstance(ref, int) else None,
        seq=int(record.get("seq") or 0),
    )


async def _lines(key: str):
    """Yield complete lines from a stored object without buffering it.

    A trailing fragment with no newline is yielded at the end: a Job killed
    mid-write leaves exactly that, and the caller's parser skips it if it does
    not parse.
    """
    storage = get_storage()
    buffer = b""
    async for chunk in storage.get_stream(key):
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            yield line
    if buffer:
        yield buffer


def _resolve_tmpdir() -> str | None:
    """The configured ephemeral PVC dir if it exists, else None.

    None falls back to the system tempdir, which is right for tests and local
    dev without a PVC mount. The partition buffers a whole run's stream split
    across hosts, which is exactly the >10 MB case the PVC rule exists for --
    the API pod's `/tmp` is RAM-backed.
    """
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


class _Partitioner:
    """Buffers each host's events to its own file, then uploads one object each.

    Written during `ingest`'s single pass rather than by re-reading the stream
    per host: N passes over a multi-MB object to produce N objects is the cost
    the partition exists to remove, paid at write time instead of read time.

    Buffered to DISK and not to memory. A fleet's stream is the one artifact
    here that is unbounded by design, and holding it split across a dict of
    lists would make the API pod's memory scale with the size of the operator's
    estate.

    Lines go in VERBATIM -- the bytes as the callback wrote them -- so a host's
    partition is a byte-exact subsequence of the whole stream and a reader
    parses both the same way. Re-serialising the parsed record would silently
    normalise key order and number formatting between the two.
    """

    def __init__(self, workspace_id: str, run_id: str) -> None:
        self._workspace_id = workspace_id
        self._run_id = run_id
        self._dir = tempfile.mkdtemp(prefix="tp-hostevents-", dir=_resolve_tmpdir())
        # host -> that host's spool file path. The PATH, not an open handle:
        # `add` appends inside a `with`, so no descriptor outlives the call and
        # the only thing `cleanup` has left to release is the directory.
        #
        # This used to cache handles behind a 64-entry LRU to amortise the
        # `open`, and that shape cost three CodeQL py/file-not-closed alerts
        # and two dismissals -- 550, then 558 twelve lines further down, then
        # 566 once the file moved again. The property was real and tested:
        # every handle was closed by LRU eviction, by `flush`, or by an
        # ExitStack callback. But it lived across four members plus a `with` on
        # the caller, which no analyser can verify and a reader has to
        # reconstruct -- and a suppression that must be re-applied every time
        # the file is edited is not a disposition. So the lifetime is now one
        # line long and both can check it by looking at it.
        #
        # The trade is an open/close pair per written line instead of one per
        # host per eviction cycle. These are appends to a local spool file that
        # is read back and uploaded moments later, so the syscall pair is noise
        # beside the HTTP stream being parsed and the object being PUT. It also
        # removes the per-host ExitStack callback whose growth #2425 section 4
        # recorded as a cost in its own right.
        self._paths: dict[str, str] = {}

    def add(self, host: str, raw: bytes) -> None:
        path = self._paths.get(host)
        if path is None:
            # One file per host, named by ordinal rather than by hostname: a
            # host name is not a safe filename, and the mapping is held in the
            # dict rather than recovered from the directory.
            path = os.path.join(self._dir, f"{len(self._paths)}.jsonl")
            self._paths[host] = path
        # "ab" unconditionally: the directory is made fresh per run, so the
        # first append to a new path creates the file and there is never prior
        # content to truncate.
        with open(path, "ab") as handle:
            handle.write(raw)
            if not raw.endswith(b"\n"):
                handle.write(b"\n")

    async def flush(self) -> int:
        """Upload one object per host. Returns how many were written."""
        storage = get_storage()
        written = 0
        for host, path in self._paths.items():
            key = host_events_host_key(self._workspace_id, self._run_id, host)
            try:
                with open(path, "rb") as fh:
                    await storage.put(key, fh.read(), content_type="application/x-ndjson")
                written += 1
            except Exception:
                # A partition is a read-side convenience: the whole-run stream
                # and every row are already stored, so losing one must not fail
                # the ingest and lose those too. The drill-down degrades to the
                # whole stream, which is what it was before the partition.
                logger.warning(
                    "failed to write a host's event partition",
                    run_id=self._run_id,
                    exc_info=True,
                )
        return written

    def __enter__(self) -> _Partitioner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Remove the spool directory, on every path.

        The only call site holds this object in a `with`, so this runs whether
        `ingest` returns or raises -- the property
        `TestTheSpoolDirectoryIsAlwaysRemoved` pins by driving the real `ingest`
        with a failure AFTER the stream. There are no descriptors left to
        release: every `open` in this module is a `with`, which
        `test_every_open_in_this_module_is_a_with` asserts over the source so
        the CodeQL class cannot return by someone reintroducing a cached
        handle.
        """
        shutil.rmtree(self._dir, ignore_errors=True)


#: How far into the stream `reported_inventory_hosts` reads before giving up.
#:
#: The record is the third line of a well-formed stream -- `playbook_start`,
#: `play_start`, `inventory` -- so a bound makes the read O(1) rather than
#: proportional to a fleet's worth of task results. Generous rather than tight
#: because the cost of being wrong is asymmetric: too tight and the denominator
#: silently reverts to the artifact's short host set, which is the failure the
#: record exists to prevent. The miss is logged either way.
_INVENTORY_RECORD_SCAN_LINES = 100


async def reported_inventory_hosts(run: Run) -> set[str]:
    """The inventory host set the EE's own callback reported, or an empty set (#2033).

    Read from the HEAD of the uploaded event stream rather than from a column or
    a dedicated endpoint, because the stream already travels and the record is
    its third line. One owner of the stream's format, which is this module.

    It matters only for a workspace that resolves dynamic inventory sources on
    the runner: the API's artifact then lists what the API could read, so the
    hosts a cloud plugin discovered are absent from it. Where the API resolved
    everything, this agrees with the artifact and adds nothing.

    Empty for every ordinary reason -- no stream yet, a Job that died before
    uploading, an older runner image whose callback does not emit the record, a
    callback that could not reach the inventory. The caller unions rather than
    replaces, so an empty answer costs nothing and a short answer cannot shrink
    the denominator.
    """
    key = host_events_key(str(run.workspace_id), str(run.id))
    seen = 0
    try:
        async for raw in _lines(key):
            if not raw.strip():
                continue
            seen += 1
            if seen > _INVENTORY_RECORD_SCAN_LINES:
                break
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict) or record.get("kind") != "inventory":
                continue
            hosts = record.get("hosts")
            if isinstance(hosts, list):
                return {str(h) for h in hosts if h}
            return set()
    except Exception:  # noqa: BLE001 - any storage error degrades to "not reported"
        return set()
    if seen:
        logger.info(
            "no inventory record in the head of the event stream",
            run_id=str(run.id),
            scanned=min(seen, _INVENTORY_RECORD_SCAN_LINES),
        )
    return set()


async def ingest(db: AsyncSession, run: Run) -> tuple[int, int]:
    """Read the run's uploaded event stream into rows. Returns (hosts, events).

    Idempotent by replacement rather than by merge: a re-upload of the same run
    replaces what is there, because the stream is append-only within one run and
    a partial second ingest alongside the first would double every event. A
    composite run's configure uploads once.
    """
    key = host_events_key(str(run.workspace_id), str(run.id))

    existing_results = await db.execute(select(RunHostResult).where(RunHostResult.run_id == run.id))
    for row in existing_results.scalars().all():
        await db.delete(row)
    existing_events = await db.execute(select(RunHostEvent).where(RunHostEvent.run_id == run.id))
    for row in existing_events.scalars().all():
        await db.delete(row)

    recap: dict[str, dict[str, Any]] = {}
    unreachable_hosts: set[str] = set()
    events: list[RunHostEvent] = []
    malformed = 0
    truncated = False
    # The partitioner owns a temp directory holding a full copy of the stream
    # split by host, so its lifetime is a `with` rather than a `try` around
    # `flush` alone: a storage error mid-stream, or a failing `db.flush`, used
    # to leave the directory behind, which is exactly the PVC-filling failure
    # the cleanup exists to prevent. It no longer holds open descriptors --
    # each append is its own `with` -- so the directory is all there is to
    # release.
    #
    # "At completion", per #1928: the upload happens after the pass, so a
    # partition exists only for a stream that was read all the way through.
    with _Partitioner(str(run.workspace_id), str(run.id)) as partitions:
        async for raw in _lines(key):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            # Before the kind filters below: they keep only `failed`/`unreachable`
            # as ROWS, while a host's partition is everything that happened to it.
            # Putting this after them would partition the failures alone, which is
            # what the rows already are.
            host_of_record = record.get("host")
            if isinstance(host_of_record, str) and host_of_record:
                partitions.add(host_of_record, raw)
            kind = record.get("kind")
            if kind == "stats":
                reported = record.get("recap")
                if isinstance(reported, dict):
                    recap = {str(h): s for h, s in reported.items() if isinstance(s, dict)}
                continue
            if kind not in ROW_KINDS:
                continue
            if kind == "unreachable":
                unreachable_hosts.add(str(record.get("host") or ""))
            if len(events) >= MAX_EVENT_ROWS:
                truncated = True
                continue
            events.append(_event_row(run, record))

        # Every host that failed or was unreachable gets a row even when the stats
        # event never arrived -- a Job killed mid-play writes events and no recap,
        # and a host whose only trace is a failure must not vanish.
        for event in events:
            recap.setdefault(event.host, {})

        for host, summary in recap.items():
            row = _recap_row(run, host, summary)
            # From the EVENT, not the counters: with `ignore_unreachable: true`
            # ansible reports `unreachable=0 ignored=1` while the unreachable hook
            # fires, so the recap alone cannot answer this. Measured on 2.18.3.
            row.saw_unreachable = host in unreachable_hosts or row.unreachable > 0
            db.add(row)
        for event in events:
            db.add(event)
        await db.flush()

        # "At completion", per #1928: after the pass, so a partition exists only for
        # a stream that was read all the way through. `cleanup` is in a `finally`
        # because the temp directory holds a copy of the whole stream split by host
        # -- leaving it behind on an error would fill the PVC one failed ingest at a
        # time, which is the failure mode the buffering was supposed to avoid.
        partitioned = await partitions.flush()

    if malformed or truncated:
        logger.warning(
            "host-event stream was partly unreadable",
            run_id=str(run.id),
            malformed_lines=malformed,
            event_rows_capped=truncated,
            cap=MAX_EVENT_ROWS,
        )
    logger.info(
        "ingested per-host results",
        run_id=str(run.id),
        hosts=len(recap),
        event_rows=len(events),
        partitions=partitioned,
    )
    return len(recap), len(events)


async def list_results(db: AsyncSession, run: Run) -> list[RunHostResult]:
    """Every per-host summary for a run, host order."""
    result = await db.execute(
        select(RunHostResult)
        .where(RunHostResult.run_id == run.id)
        .order_by(RunHostResult.host.asc())
    )
    return list(result.scalars().all())


async def list_events(db: AsyncSession, run: Run, *, host: str | None = None) -> list[RunHostEvent]:
    """A run's failure and unreachable events, optionally one host's.

    Ordered by the plugin's own sequence rather than by timestamp: under
    `forks` several hosts complete within the same millisecond, so a timestamp
    ordering ties and ansible's own order is lost.
    """
    query = select(RunHostEvent).where(RunHostEvent.run_id == run.id)
    if host:
        query = query.where(RunHostEvent.host == host)
    result = await db.execute(query.order_by(RunHostEvent.seq.asc()))
    return list(result.scalars().all())


# ── A failed configure's summary, for the run's error message (#2525) ──────

#: How many hosts the summary names before it says "+N more".
#:
#: Three rather than all of them because this lands in `run.error_message`,
#: which a notification body and a list tooltip both render: a playbook that
#: failed on four hundred hosts must not produce a four-hundred-line error. The
#: Hosts tab is where the whole set is read.
MAX_SUMMARY_HOSTS = 3

#: A module message in the summary is a recognition aid, not the payload. The
#: whole result is in the event stream and, when it was large, in the sidecar.
_SUMMARY_MSG_LIMIT = 300

#: Ansible's output carries colour -- `ANSIBLE_FORCE_COLOR` is forced on so the
#: log viewer's colour toggle works on a configure (#2413 F18) -- so a module
#: message can arrive with escape sequences in it. The error message is rendered
#: as plain text in a dozen places that have no renderer for them.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: `failed` and `unreachable` are the only kinds that become rows (`ROW_KINDS`)
#: and they read differently: a host that failed ran something, a host that was
#: unreachable did not. Keyed on the same two so a third kind cannot arrive and
#: be summarised as "failed".
_KIND_VERB: dict[str, str] = {"failed": "failed", "unreachable": "unreachable"}


def _one_line(text: str) -> str:
    """A module message as one bounded line: no escapes, no control characters."""
    text = _ANSI.sub("", text or "")
    # `split()` with no argument splits on every run of whitespace, so this also
    # removes the newlines, tabs and stray control whitespace a module's `msg`
    # can carry -- the whole point being that this lands on one line.
    text = " ".join(text.split())
    if len(text) > _SUMMARY_MSG_LIMIT:
        text = text[: _SUMMARY_MSG_LIMIT - 1] + "…"
    return text


def _where(event: RunHostEvent) -> str:
    """`host at "task"`, or the bare host when the event names no task."""
    return f'{event.host} at "{event.task_name}"' if event.task_name else event.host


def render_failure_summary(events: Sequence[RunHostEvent]) -> str | None:
    """What a failed configure's hosts say, or None when they say nothing.

    `events` is a run's rows in the plugin's own sequence order, which is what
    makes the FIRST event per host the one reported: ansible's first failure on
    a host is the cause and the ones after it are usually consequences of it.

    **An `ignored` event is not a failure of the run.** `ignore_errors: true`
    means the playbook carried on, and the recap counts it under `ignored`
    rather than `failures` -- so including one would report hosts as having
    failed a run that failed for another reason entirely, or even one that
    succeeded. They stay in the rows, where the Hosts tab shows them.

    Pure, and separate from the query, because every judgement here is about
    rendering: the query is one `select`, and this is the part worth pinning.
    """
    by_kind: dict[str, dict[str, RunHostEvent]] = {}
    for event in events:
        if event.ignored:
            continue
        if event.kind not in _KIND_VERB:
            continue
        # First event per host wins -- see the docstring.
        by_kind.setdefault(event.kind, {}).setdefault(event.host, event)

    blocks: list[str] = []
    # `ROW_KINDS` order, so a run with both kinds reads the same way every time.
    for kind in ROW_KINDS:
        hosts = by_kind.get(kind)
        if not hosts:
            continue
        count = len(hosts)
        head = f"{count} host{'' if count == 1 else 's'} {_KIND_VERB[kind]}"
        shown = list(hosts.values())[:MAX_SUMMARY_HOSTS]

        lines: list[str] = []
        if count == 1:
            # The shape #2525 asks for, and the overwhelmingly common one.
            lines.append(f"{head}: {_where(shown[0])}")
            message = _one_line(shown[0].msg)
            if message:
                lines.append(f"  {message}")
        else:
            lines.append(f"{head}:")
            for event in shown:
                lines.append(f"  {_where(event)}")
                message = _one_line(event.msg)
                if message:
                    lines.append(f"    {message}")
            if count > len(shown):
                lines.append(f"  +{count - len(shown)} more")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else None


async def failure_summary(db: AsyncSession, run: Run) -> str | None:
    """A failed configure's host summary for `run.error_message`, or None.

    Reads the ROWS rather than the stream: they are already the two kinds a
    reader needs, they are ordered by the plugin's own sequence, and the
    question is answerable without fetching an object.

    **The rows are already redacted.** `uploads.upload_host_events` puts the
    whole capture through the run's redactor and refuses the upload outright
    when it cannot (#1934), so every `msg` here went through it before the API
    saw it. There is no second pass for the API to make: the redaction registry
    lives in the runner process, built from the credentials that run was handed,
    and the API never holds one.
    """
    return render_failure_summary(await list_events(db, run))
