"""Ingest and read a configure's per-host results (#1928).

The callback plugin writes one JSON object per line on the runner's shared
volume; the runner uploads that stream; this turns the two kinds that belong in
Postgres into rows and leaves everything else in the object.

**What comes into Postgres, and why only that.** Measured on ansible-core
2.18.3: a four-host play with one 25-item loop produced 107 events and 79 KB of
result payloads, 75 of them loop items, with a single `gather_facts` result at
16.5 KB per host. Row-per-event at that rate is the scaling pain large AWX
installs are known for, so the rows are the per-host summary -- one per
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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, RunHostEvent, RunHostResult
from terrapod.logging_config import get_logger
from terrapod.storage import get_storage
from terrapod.storage.keys import host_events_key

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
