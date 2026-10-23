"""Capture per-host, per-task events from a configure (#1928).

**This file is not Terrapod Python.** It runs inside the execution-environment
container, under the EE's own interpreter and its own ansible-core -- anything
from 3.10 to 3.14 across the ten published EE lines (#2154) -- so it imports the
standard library and `ansible` and nothing else. It is shipped as a DATA ASSET
rather than as a module of ours: `Dockerfile.runner` copies it to a destination
outside `terrapod/`, which is what keeps it out of the runner-image import gates
(the same treatment `saas_known_hosts` gets, for the same reason). Nothing in
Terrapod imports it.

It is a **notification** callback, not a stdout one. Ansible permits exactly one
`stdout_callback` and many notification callbacks, which is what lets this
coexist with ansible's own renderer -- measured on 2.18.3: 107 events captured
while `default` produced byte-normal output. The human log's format belongs to
#2144's stdout callback; this one never writes to stdout.

`CALLBACK_NEEDS_ENABLED = False` is load-bearing and not a convenience. Measured
against an `ansible.cfg` that sets both `callback_plugins` and
`callbacks_enabled`:

    ANSIBLE_CALLBACK_PLUGINS=<ours>                      ours YES, theirs GONE
    $HOME/.ansible/plugins/callback (no env var)         ours no,  theirs YES
    a collection + ANSIBLE_COLLECTIONS_PATH              ours no,  theirs YES
    a collection + ANSIBLE_CALLBACKS_ENABLED by FQCN     ours YES, theirs GONE
    ANSIBLE_CALLBACK_PLUGINS=<ours>:<their own path>     ours YES, theirs YES

An operator's `callback_plugins` REPLACES the default search path, so the user
plugin directory and the collection route cannot reach us; and writing
`ANSIBLE_CALLBACKS_ENABLED` replaces their list, silently disabling every
notification callback they run. Only the last form does both, so the phase reads
their effective path with `ansible-config dump` and prepends ours -- and because
this plugin needs no enabling, their `callbacks_enabled` is never touched. That
is the same promise `configure.py` makes about never replacing their
`ansible.cfg`, applied to the one setting we have to extend.

## What it can and cannot know

**It cannot tell you which hosts a play targeted.** `v2_playbook_on_play_start`
reports the host PATTERN (`['web:db']`), never the expansion, and it fires once
per batch under `serial:`. Measured with `serial: 1` and `any_errors_fatal: true`
over four hosts failing in the first batch: the PLAY RECAP lists ONE host, this
callback hears about ONE, and `stats.processed` holds ONE. Three hosts that were
in scope appear nowhere at all. So the denominator is the run's resolved
inventory artifact (#2021) and nothing here can substitute for it.

**The recap cannot report unreachability.** With `ignore_unreachable: true`,
`v2_runner_on_unreachable` fires while the summary reads `unreachable=0
ignored=1`. That is why `saw_unreachable` is emitted from the EVENT rather than
derived from the stats, and why the per-host failure rows are load-bearing rather
than an optimisation.

## The two files, and why oversized payloads go to the second

Everything is captured; nothing is truncated. But a single `gather_facts` result
is 16.5 KB per host (measured; a Linux host with more mounts and NICs is larger),
and a 25-iteration loop over four hosts produced 75 of 107 events. So a payload
over `_SPILL_BYTES` is written to a sidecar file and referenced by id, which
keeps the file a host filter has to scan small while losing nothing.

**One sidecar per run, not one per payload.** The retention sweep deletes a fixed
key list per run (`artifact_retention_service._RUN_ARTIFACT_KEYS`) and has no
prefix enumeration, so an object per oversized payload would leak for ever.
"""

from __future__ import annotations

import json
import os
import time

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = r"""
    name: terrapod_events
    type: notification
    short_description: Capture per-host, per-task events for Terrapod
    description:
      - Writes one JSON object per line describing every task result ansible
        reports, plus the final per-host summary.
      - Terrapod's own plugin. It writes no stdout and changes no rendering.
    requirements: []
"""

#: Where the event stream is written. The phase passes this; the fallback matches
#: `job_template._EE_CALLBACK_DIR` so a probe against a real EE needs no Job.
_PATH_ENV = "TP_HOST_EVENTS_PATH"
_DEFAULT_PATH = "/tp/callback/events.jsonl"

#: A payload above this goes to the sidecar. 64 KiB is deliberately ABOVE the
#: measured 16.5 KB of a `gather_facts` result: facts are the normal case and
#: spilling every host's would move the bulk of an ordinary run into the file
#: that is meant to hold the exceptions.
_SPILL_BYTES = 64 * 1024

#: Owner-only. The orchestrator reads what this writes, and both containers run
#: as the SAME pinned uid (`_ee_security_context`, #1928) -- so the group bit
#: would carry none of the contract and granting it is a wider permission than
#: the work needs. It was 0640 while the EE's uid was unpinned.
#:
#: Set explicitly rather than left to the umask, which is the EE image's to
#: choose: a default 022 would create this 0644, i.e. world-readable.
_MODE = 0o600


class CallbackModule(CallbackBase):
    """Emit one JSON line per event. Never raises into ansible."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "terrapod_events"
    #: See the module docstring: enabling by name would replace the operator's
    #: own `callbacks_enabled`, so this plugin must load from the path alone.
    CALLBACK_NEEDS_ENABLED = False

    def __init__(self) -> None:
        super().__init__()
        self._path = os.environ.get(_PATH_ENV) or _DEFAULT_PATH
        self._large_path = _sidecar_path(self._path)
        self._seq = 0
        self._spills = 0
        self._events = _open_append(self._path)
        self._large = None
        self._play = ""

    # -- emission ---------------------------------------------------------
    def _write(self, handle, record: dict) -> None:
        handle.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
        handle.flush()

    def _emit(self, kind: str, **fields) -> None:
        """Append one event. A capture failure must never fail the configure.

        An exception raised from a callback propagates and aborts the play, which
        would mean a full-disk shared volume turning a working configure into a
        failed one -- the reporting path breaking the thing it reports on. The
        events are evidence, so losing a line is survivable and losing the run is
        not.
        """
        if self._events is None:
            return
        self._seq += 1
        record = {"seq": self._seq, "kind": kind, "at": round(time.time(), 3), **fields}
        try:
            self._write(self._events, record)
        except OSError:
            self._events = None

    def _spill(self, payload: dict) -> int | None:
        """Write an oversized payload to the sidecar; return its reference id."""
        if self._large is None:
            self._large = _open_append(self._large_path)
        if self._large is None:
            return None
        self._spills += 1
        try:
            self._write(self._large, {"ref": self._spills, "payload": payload})
        except OSError:
            self._large = None
            return None
        return self._spills

    def _result(self, kind: str, result, **extra) -> None:
        task = result._task
        payload = result._result if isinstance(result._result, dict) else {}
        encoded = json.dumps(payload, default=str, separators=(",", ":"))
        fields = {
            "host": result._host.get_name(),
            "play": self._play,
            "task": task.get_name(),
            "action": task.action,
            "task_uuid": str(task._uuid),
            "changed": bool(payload.get("changed")),
            "bytes": len(encoded),
            **extra,
        }
        if len(encoded) > _SPILL_BYTES:
            ref = self._spill(payload)
            fields["payload_ref"] = ref
            # Keep the one field a reader needs even when the body is elsewhere.
            if isinstance(payload.get("msg"), str):
                fields["msg"] = payload["msg"][:2000]
        else:
            fields["payload"] = payload
        self._emit(kind, **fields)

    # -- ansible's hooks --------------------------------------------------
    def v2_playbook_on_start(self, playbook) -> None:
        self._emit("playbook_start", playbook=getattr(playbook, "_file_name", ""))

    def v2_playbook_on_play_start(self, play) -> None:
        self._play = play.get_name() or ""
        # The PATTERN, not the host set: see the module docstring.
        self._emit("play_start", play=self._play, pattern=_pattern(play))

    def v2_playbook_on_task_start(self, task, is_conditional) -> None:  # noqa: ARG002
        self._emit(
            "task_start",
            play=self._play,
            task=task.get_name(),
            action=task.action,
            task_uuid=str(task._uuid),
        )

    def v2_runner_on_ok(self, result) -> None:
        self._result("ok", result)

    def v2_runner_on_failed(self, result, ignore_errors=False) -> None:
        self._result("failed", result, ignored=bool(ignore_errors))

    def v2_runner_on_skipped(self, result) -> None:
        self._result("skipped", result)

    def v2_runner_on_unreachable(self, result) -> None:
        self._result("unreachable", result)

    def v2_runner_item_on_ok(self, result) -> None:
        self._result("item_ok", result)

    def v2_runner_item_on_failed(self, result) -> None:
        self._result("item_failed", result)

    def v2_runner_item_on_skipped(self, result) -> None:
        self._result("item_skipped", result)

    def v2_playbook_on_stats(self, stats) -> None:
        """The per-host summary, which is the one row per `(run, host)`.

        `stats.processed` holds only the hosts ansible heard about, which is the
        measured gap the module docstring records -- the run's resolved inventory
        is what says who was in scope.
        """
        recap = {host: stats.summarize(host) for host in sorted(stats.processed)}
        self._emit("stats", recap=recap)
        for handle in (self._events, self._large):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._events = None
        self._large = None


def _pattern(play) -> str:
    """The play's host pattern as written, joined when ansible gives a list."""
    hosts = getattr(play, "hosts", "")
    if isinstance(hosts, (list, tuple)):
        return ",".join(str(h) for h in hosts)
    return str(hosts or "")


def _sidecar_path(path: str) -> str:
    """`…/events.jsonl` -> `…/events-large.jsonl`."""
    base, _, suffix = path.rpartition(".jsonl")
    return (base + "-large.jsonl") if base and not suffix else path + "-large"


def _open_append(path: str):
    """Line-buffered append, owner-only, or None if it cannot be opened."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handle = open(path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return None
    try:
        os.chmod(path, _MODE)
    except OSError:
        pass
    return handle
