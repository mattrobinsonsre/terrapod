"""The one producer of every run-scoped SSE event, published after the commit.

Three payloads reach the run detail page and the workspace page:
`run_status_change`, `run_updated` and `log_updated`. They used to be built and
published in two different modules -- the status one in `run_service`, the other
two in the `run_artifacts` router -- each inline, each inside the transaction that
caused it.

**Inline was the defect.** An event on the wire before its transaction commits is
one a subscriber can read ahead of: the page refetches on receipt and reads the
row as it was *before* the change the event describes. How badly that bites
depends on who commits and when -- an API request commits as soon as its handler
returns, while the run reconciler commits once outside its per-run loop -- and it
bit worst where nothing published afterwards to correct it, which is a configure's
terminal transition (#2720).

So every one of them is deferred through `db/post_commit.py`, and the entry points
here are the only way to emit one. That is asserted rather than asked for: see
`tests/api/test_run_column_writes_notify.py`.

**Why a service module rather than leaving two homes.** The router's publishers
could not be reached from the service layer, which is why two real writes
announced nothing at all -- the composite apply/configure hand-over, and the
reconciler's fallback that promotes a live log to storage. One producer in
`services/` is reachable from both a router and a service, and means the payload
shape has a single definition.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from terrapod.db import post_commit

logger = structlog.get_logger(__name__)


async def _publish(channels: list[str], payload: str) -> None:
    """Fan one payload to each channel.

    Bounding and failure handling belong to `post_commit`, which logs the `what`
    of anything that fails -- so this takes no description of its own.
    """
    from terrapod.redis.client import publish_event

    for channel in channels:
        await publish_event(channel, payload)


def status_change_channels(workspace_id: str) -> list[str]:
    """The three channels a status change fans to.

    Named so it can be asserted directly. The fanout used to be three successive
    `publish_event` calls inside the publisher, provable only by patching Redis
    and counting awaits — which is a test of the mock as much as of the list.
    """
    from terrapod.redis.client import (
        ADMIN_EVENTS_CHANNEL,
        RUN_EVENTS_PREFIX,
        WORKSPACE_LIST_EVENTS_CHANNEL,
    )

    return [
        f"{RUN_EVENTS_PREFIX}{workspace_id}",
        ADMIN_EVENTS_CHANNEL,
        WORKSPACE_LIST_EVENTS_CHANNEL,
    ]


def defer_status_change(
    db: Any, *, run_id: str, workspace_id: str, old_status: str, new_status: str
) -> None:
    """A run moved to a new state.

    Three channels: the per-workspace one the run detail and workspace detail
    pages read, the admin one, and the workspace-list one.
    """
    payload = json.dumps(
        {
            "event": "run_status_change",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "old_status": old_status,
            "new_status": new_status,
        }
    )
    channels = status_change_channels(workspace_id)
    what = f"run_status_change {old_status or '(new)'}->{new_status}"
    post_commit.run_after_commit(db, lambda: _publish(channels, payload), what=what)


def run_updated_payload(*, workspace_id: str, run_id: str, changed: str) -> str:
    """The `run_updated` wire shape. One definition, two delivery timings.

    The router publishes it INLINE from an API handler, where the commit follows
    as soon as the handler returns and the window is the remainder of that
    handler. The service layer publishes it DEFERRED, because its callers commit
    much later -- the reconciler commits once outside its per-run loop. Sharing
    the builder is what stops the two drifting; `tests/api/test_run_column_writes_notify.py`
    asserts there is no third producer.
    """
    return json.dumps(
        {
            "event": "run_updated",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "changed": changed,
        }
    )


def log_updated_payload(*, workspace_id: str, run_id: str, phase: str) -> str:
    """The `log_updated` wire shape. See `run_updated_payload` on the two timings."""
    return json.dumps(
        {
            "event": "log_updated",
            "run_id": run_id,
            "workspace_id": workspace_id,
            "phase": phase,
        }
    )


def defer_run_updated(db: Any, *, workspace_id: str, run_id: str, changed: str) -> None:
    """A run's own columns changed without a state transition.

    The page refetches the run on `run_status_change`, so a column written
    afterwards -- which is what every artifact upload does, and what the composite
    hand-over does -- is invisible until something says so. `changed` names what
    moved, so a consumer can be selective without re-reading the whole run.

    A distinct event rather than reusing `run_status_change`: no status changed,
    and the page keys on that name to drive log followers and phase transitions.
    """
    from terrapod.redis.client import RUN_EVENTS_PREFIX

    payload = run_updated_payload(workspace_id=workspace_id, run_id=run_id, changed=changed)
    what = f"run_updated {changed}"
    post_commit.run_after_commit(
        db, lambda: _publish([f"{RUN_EVENTS_PREFIX}{workspace_id}"], payload), what=what
    )


def defer_log_updated(db: Any, *, workspace_id: str, run_id: str, phase: str) -> None:
    """A fresh log artifact landed in storage.

    The authoritative final log is uploaded after the run has already turned
    terminal, and the UI only re-fetches a log when one of these arrives -- so
    without it the trailing bytes sit in storage, invisible until a reload.
    """
    from terrapod.redis.client import RUN_EVENTS_PREFIX

    payload = log_updated_payload(workspace_id=workspace_id, run_id=run_id, phase=phase)
    what = f"log_updated {phase}"
    post_commit.run_after_commit(
        db, lambda: _publish([f"{RUN_EVENTS_PREFIX}{workspace_id}"], payload), what=what
    )
