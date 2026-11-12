"""One result block per task per host, coloured by the result (#2513, #2144).

**This file is not Terrapod Python**, for the same reasons `terrapod_events.py`
is not: it runs inside the execution-environment container, under the EE's own
interpreter and its own ansible-core -- anything from 3.10 to 3.14 across the
ten published EE lines (#2154) -- so it imports the standard library and
`ansible` and nothing else. It ships as a DATA ASSET rather than as a module of
ours; nothing in Terrapod imports it.

It is a **stdout** callback, and it is the only one Terrapod sets. Ansible
permits exactly one, so this REPLACES an operator's own `stdout_callback` where
they set one. That is deliberate (user, 2026-10-23): *"We have to control the
output format because it's our responsibility to make it visible to the UI and
api."* The platform owns the log's shape for the same reason it owns redaction
-- the UI and the API are its contract, not the playbook author's. Note the
scope: we still never replace the operator's `ansible.cfg` itself, which is what
`configure.py`'s promise is about; we set one environment variable.

## Why a block rather than a per-line prefix

v1 rendered exactly as the `default` callback does and prefixed every emitted
line with its host. That attributed every line -- which is the hard part, and
is why this is a callback rather than a parser over the combined stream -- but
it left the LAYOUT as `default`'s, with a column of host names down the left.
The task banner, the host's result and any timing line from another callback
stayed three separate items with nothing pairing them up, and every line was a
host field longer for it.

v2 makes the **block** the unit of output. One block per task per host, carrying
the four things a reader is looking for -- when, which host, what happened,
which task -- on its header line, with everything else that result produced
indented beneath it. There is no second format and no toggle (user, 2026-11-11):
*"We own the ansible output. It should be complete but notwithstanding the
original request we are entitled to design our own output structure."* The
obligation that replaces the toggle is **completeness**: nothing ansible emits
may be lost, which is what the capture below is for.

## Why a callback rather than parsing the combined stdout

Because the combined stream cannot be attributed, measured on ansible-core
2.21.5 over a 3-host fixture: only **12 of 41** non-blank lines carry `[host]`
at all (6 of 24 under `strategy: free`). Three classes defeat a text parser
outright, and they are exactly the lines someone filters in order to debug:

* the `[ERROR]:` block, its `Origin:` path and the quoted YAML excerpt are
  emitted BEFORE the `fatal: [web2]` line that names the host;
* `[WARNING]` lines name the host in prose, in single quotes, out of order;
* `...ignoring` after an ignored failure names no host.

A callback holds the host structurally for all of them, because it is handed
the result object -- and in v2 all three land INSIDE the failing host's block,
which is what the issue asked for.

## The format

A self-identifying header, then blocks::

    #terrapod-host-log v2
    [2026-11-11 10:28:27]  ~                 play     PLAY [configure]
    [10:28:30 +2.9s]       web1.example.com  changed  platform : Deploy the app
    [10:31:42 +0.0s]       web1.example.com  FAILED   platform : Check the endpoint
                      {
                          "msg": "connection refused"
                      }

A **block header** is ``[<time>]  <host>  <result>  <label>``. A **body line**
is indented by `INDENT` and belongs to the block above it. Anything else --
another callback's line, a core warning printed outside any task -- is emitted
exactly as it was written and read back as host-less context, which is what
"whatever an operator's own `profile_tasks` / `timer` callback emits, left as
it is" means in practice.

The whole block is coloured by the result, using ansible's own `COLOR_OK`,
`COLOR_CHANGED`, `COLOR_ERROR`, `COLOR_UNREACHABLE` and `COLOR_SKIP`. Ansible's
`stringc` wraps each LINE in its own SGR pair, so a filtered view that keeps
some blocks and drops others can never leave a colour unterminated.

**The header is NOT at offset 0 of the stored log**, and a parser must not
assume it is. `ansible_collections.install` writes galaxy output to the same
file first, and the orchestrator's own structlog lines are rolled in around it,
so the file is legitimately part untagged. Everything before the header is
host-less context.

`~` is the host-less marker -- the PLAY banner, the PLAY RECAP, play-level
output. It is explicit rather than "no host field" because a filtered view still
has to show those, and because the absence of a field is also what an older
runner's log looks like.

The marker cannot collide with a host name because `~` is in the escape set:
any host actually called `~` renders as `%7E`. Escaping rather than validating
is forced -- `validate_host_name` refuses whitespace and `--limit`'s operators
but not `|`, and a host arriving from a **git** inventory source never passes
through it at all, so the format can assume nothing about a host name. The
escape of **whitespace** is what the grammar actually rests on: the host is the
first whitespace-delimited field after the bracketed time, so a name carrying a
space would otherwise be read as a host and a result.

## Nothing is lost, and the mechanism is capture rather than reimplementation

Each result hook pushes a capture frame, calls `super()`, and pops it. Whatever
`default` chose to print lands in the frame and becomes the block's body, so
every option it reads (`display_ok_hosts`, `display_skipped_hosts`,
`result_format`, `show_task_path_on_failure`, verbosity) keeps working and is
not restated here. **An empty frame means `default` chose to say nothing** --
an `ok` host with `display_ok_hosts: false`, a `TaskInclude` -- and then no
block is emitted either. `default` decides whether to speak; we decide how it
looks.

The one line we do rewrite is `default`'s own status line (`changed: [web1]`,
`fatal: [web2]: FAILED! => {...}`), because the block header already carries
its host and its result. It is matched against THIS result's host label, so a
line that is not it cannot be trimmed by accident, and anything after the
`=> ` -- the dumped result, which is the part that matters -- is kept.

TASK banners are the one thing deliberately dropped, and nothing is lost by it:
their entire content is the task name, which every block carries in its label.
Keeping them would reinstate exactly the "the task and its result are still two
items" complaint v2 exists to fix.
"""

from __future__ import annotations

import re
import time

from ansible import constants as C
from ansible.plugins.callback.default import CallbackModule as DefaultCallback

#: **Load-bearing, and its absence is not a documentation nit.** `default` reads
#: its behaviour through `self.get_option(...)` -- `display_ok_hosts`,
#: `display_skipped_hosts`, `show_per_host_start`, `result_format` and the rest
#: -- and an option is only registered for a plugin that declares it. A subclass
#: with no DOCUMENTATION therefore inherits the CODE and not the OPTIONS, so
#: every one of those raises and ansible turns each into
#: `[WARNING]: Failure using method (v2_runner_on_ok) in callback plugin ...`.
#: Measured: the first probe run emitted eight such warnings and no task output
#: at all. Extending the same two fragments `default` extends is what fixes it,
#: and it is also what keeps an operator's existing `ansible.cfg` options for
#: those settings working under our renderer.
DOCUMENTATION = """
    name: terrapod_stdout
    type: stdout
    short_description: one result block per task per host, coloured by the result
    description:
        - Emits one block per task per host. The block's header line carries the
          time and duration, the host, the result and the task, and everything
          else that result produced is indented beneath it (#2513).
        - Output belonging to no single host -- the PLAY banner, the PLAY RECAP
          -- is emitted as its own block carrying a reserved marker, because it
          is context a filtered view still has to show.
        - Rendering of the result itself is ansible's own, so every option of
          the default callback still applies.
    extends_documentation_fragment:
      - default_callback
      - result_format_callback
"""

#: Emitted once, before anything this plugin writes. A viewer keys the per-host
#: switch on finding it: an older runner image emits ansible's plain output, so
#: its absence means "no per-host view", which is the data-gating rule applied
#: to a log rather than to a column. The VERSION is part of it, so a v1 log --
#: one prefix per line -- still renders through the parser that understands it.
HEADER = "#terrapod-host-log v2"

#: Lines that belong to no single host.
HOSTLESS = "~"

#: One block header in the exact shape the grammar describes.
#:
#: It is a LITERAL rather than a call to `_tp_header_line`, deliberately: the
#: viewer's parser and the API both restate this grammar in another language
#: and cannot import it, so each reads this constant out of this file and
#: proves its own parser accepts it. `test_the_example_is_what_the_formatter_
#: produces` ties the literal back to the formatter, so the three cannot drift.
EXAMPLE_BLOCK = "[10:28:30 +2.9s]  web1              changed  platform : Deploy"

#: Every body line carries this, and a block header never starts with a space.
#: That is the whole discriminator -- no sentinel, nothing invisible, and a
#: line a human would not read as belonging to the block above it cannot be
#: attributed to it either. The width lines a body line up under the host
#: column of a block whose stamp carries no date.
INDENT = "                  "

#: Column widths, for readability alone. A longer value simply widens its own
#: line; **nothing parses by column** -- the grammar is "bracketed time, then
#: whitespace-separated fields".
_TIME_W = 16
_HOST_W = 16
_STATUS_W = 7

#: What must never appear raw in the host field. `%` is first, or escaping it
#: afterwards would double-escape the sequences introduced by the others.
_ESCAPE = ("%", "|", "~")

#: `default`'s own status line, as emitted for ONE named host. Built per result
#: from that result's host label, so a body line which merely looks like a
#: status line cannot be trimmed by accident.
_STATUS_LEAD = r"^[A-Za-z_]+: \[%s\]"

#: What follows the host label on a failure or an unreachable, before the dump.
_LEAD_TAIL = (": FAILED!", ": UNREACHABLE!", ":")

#: `default` joins its status line to the dumped result with this.
_DUMP_SEP = "=>"


def escape_host(host: str) -> str:
    """Make `host` safe for the host field, reversibly.

    Percent-escaping, so the result is still legible for an ordinary name (the
    overwhelming case is that nothing is escaped at all) and unambiguous for
    anything else.
    """
    out = host
    for ch in _ESCAPE:
        out = out.replace(ch, f"%{ord(ch):02X}")
    # Every whitespace character, not just the space: the host is the first
    # whitespace-delimited field of a block header, so a tab or a space inside
    # a name would split it into a host and a result.
    return "".join(c if not c.isspace() else f"%{ord(c):02X}" for c in out)


def format_duration(seconds: float) -> str:
    """`2.9s`, `3m12s`, `1h04m` -- short enough to sit in a column."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    whole = int(seconds)
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class CallbackModule(DefaultCallback):
    """One block per task per host. Never raises into ansible."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "terrapod_stdout"

    def __init__(self) -> None:
        super().__init__()
        self._tp_installed = False
        self._tp_header_done = False
        #: A stack, not a flag: a loop's item hooks fire from INSIDE the outer
        #: hook, and each must emit its own block rather than disappearing into
        #: the outer one's body.
        self._tp_frames: list[list[str]] = []
        #: `(task uuid, host name) -> monotonic`, so a duration is this host's
        #: time on this task and stays right under `strategy: free`, where
        #: several tasks are in flight at once.
        self._tp_started: dict[tuple, float] = {}
        self._tp_task_at = time.monotonic()
        self._tp_inner_display = None
        self._tp_inner_banner = None

    # -- the interception -------------------------------------------------
    #
    # `default` renders through `self._display.display(...)` and
    # `self._display.banner(...)`. Wrapping those two is what lets the block be
    # assembled from what it printed, including the continuation lines of a
    # multi-line result -- which is the whole reason this is a callback and not
    # a parser, since those lines carry no host of their own.
    #
    # The `Display` object is a process-wide singleton, so the wrappers are
    # installed as INSTANCE attributes on it: anything else holding the
    # singleton -- an operator's `profile_tasks`, say -- therefore also reaches
    # them, which is how its output stays in the one stream and in order.
    def _tp_install(self) -> None:
        if self._tp_installed:
            return
        self._tp_installed = True
        self._tp_inner_display = self._display.display
        self._tp_inner_banner = self._display.banner

        def display(msg="", *args, **kwargs):
            try:
                if self._tp_frames and isinstance(msg, str):
                    # `log_only` writes to the log file and not the screen, and
                    # `v2_playbook_on_stats` emits every recap row TWICE -- once
                    # colourised for the screen and once plain for the log. A
                    # frame that kept both would double the recap.
                    if not kwargs.get("log_only"):
                        self._tp_frames[-1].append(msg)
                    return None
            except Exception:  # noqa: BLE001 - never fail a play to format a log
                pass
            return self._tp_inner_display(msg, *args, **kwargs)

        def banner(msg="", *args, **kwargs):
            try:
                handled = self._tp_banner(msg)
            except Exception:  # noqa: BLE001
                handled = False
            if handled:
                return None
            return self._tp_inner_banner(msg, *args, **kwargs)

        self._display.display = display  # type: ignore[method-assign]
        self._display.banner = banner  # type: ignore[method-assign]

    def _tp_banner(self, msg) -> bool:
        """True when this banner has been handled and must not be printed.

        A banner is always one of ours to decide about: `TASK` and
        `RUNNING HANDLER` are dropped because every block carries the task name
        already, one inside a frame belongs to the block its hook is building,
        and the rest -- `PLAY`, `NO MORE HOSTS LEFT`, `DRY RUN` -- become their
        own host-less blocks.
        """
        text = (msg if isinstance(msg, str) else str(msg)).strip()
        if text.startswith(("TASK [", "RUNNING HANDLER [")):
            return True
        if self._tp_frames:
            return True
        self._tp_block(
            host=None,
            status="recap" if text.startswith("PLAY RECAP") else "play",
            label=text,
            body=(),
            color=None,
            stamp=self._tp_stamp(),
        )
        return True

    # -- writing ----------------------------------------------------------
    def _tp_emit_header(self) -> None:
        self._tp_install()
        if self._tp_header_done:
            return
        self._tp_header_done = True
        # Through the INNER display, so the header itself is not a block: it
        # describes the format rather than being an instance of it.
        try:
            self._tp_inner_display(HEADER)
        except Exception:  # noqa: BLE001
            pass

    def _tp_stamp(self, started: float | None = None) -> str:
        """The bracketed time field.

        A result carries `HH:MM:SS` and the duration, which is the shape the
        issue asked for and stays narrow enough to read down a column. The
        play-level blocks carry the full date instead, so the log says what day
        it is exactly once rather than on every line.
        """
        # `time.gmtime` rather than a datetime, for two reasons that both
        # matter here. It is explicitly UTC, so this stamp agrees with the
        # run timestamps the API serves as RFC3339 UTC instead of reporting
        # whatever zone the EE container happens to carry. And it needs no
        # tz-constant choice: this file runs under the EE interpreter,
        # anything from 3.10 to 3.14, and the alias ruff prefers for UTC
        # does not exist below 3.11.
        now = time.gmtime()
        if started is None:
            return f"[{time.strftime('%Y-%m-%d %H:%M:%S', now)}]"
        elapsed = max(0.0, time.monotonic() - started)
        return f"[{time.strftime('%H:%M:%S', now)} +{format_duration(elapsed)}]"

    def _tp_header_line(self, stamp: str, host: str, status: str, label: str) -> str:
        head = f"{stamp:<{_TIME_W}}  {host:<{_HOST_W}}  {status:<{_STATUS_W}}  {label}"
        return head.rstrip()

    def _tp_block(self, *, host, status, label, body, color, stamp) -> None:
        """Write one block: a header line, then every body line indented."""
        self._tp_emit_header()
        field = escape_host(host) if host else HOSTLESS
        lines = [self._tp_header_line(stamp, field, status, label)]
        for chunk in body:
            for line in str(chunk).split("\n"):
                # Every line, including an empty one. Indenting a blank line
                # costs eighteen invisible spaces and is what keeps its
                # attribution structural rather than a guess by the parser.
                lines.append(INDENT + line)
        try:
            # One call: ansible's `stringc` wraps each line in its own SGR pair,
            # so a filter that drops a block cannot unbalance the colour.
            self._tp_inner_display("\n".join(lines), color=color)
        except Exception:  # noqa: BLE001
            pass

    # -- result blocks ----------------------------------------------------
    @staticmethod
    def _tp_field(result, name: str):
        """`result.host` on core 2.22, `result._host` before it."""
        try:
            return getattr(result, name)
        except AttributeError:
            return getattr(result, "_" + name, None)

    def _tp_label_of(self, result) -> str:
        """`role : task name`, which is what `Task.get_name()` already gives."""
        task = self._tp_field(result, "task")
        try:
            return task.get_name().strip()
        except Exception:  # noqa: BLE001
            return ""

    def _tp_host_label(self, result) -> str:
        """Ansible's own label, which carries `host -> delegated` when delegating."""
        try:
            return self.host_label(result)
        except Exception:  # noqa: BLE001
            try:
                return self._tp_field(result, "host").get_name()
            except Exception:  # noqa: BLE001
                return ""

    def _tp_item_of(self, result) -> str:
        try:
            label = self._get_item_label(self._tp_field(result, "result"))
        except Exception:  # noqa: BLE001
            return ""
        return "" if label is None else str(label)

    def _tp_trim(self, chunk: str, host_label: str, item: str) -> str:
        """Drop `default`'s status line from a captured chunk, keeping the dump.

        Only the chunk's FIRST line, and only when it names THIS result's host:
        a `[WARNING]` line, an `...ignoring`, an indented line of a dumped
        result and anything an operator's own module printed all fail to match
        and are returned untouched. That is the completeness guarantee -- the
        trim can only ever remove something it has positively identified.
        """
        if not host_label:
            return chunk
        head, newline, rest = chunk.partition("\n")
        match = re.match(_STATUS_LEAD % re.escape(host_label), head)
        if match is None:
            return chunk
        head = head[match.end() :]
        for tail in _LEAD_TAIL:
            if head.startswith(tail):
                head = head[len(tail) :]
                break
        head = head.lstrip()
        if head.startswith(_DUMP_SEP):
            head = head[len(_DUMP_SEP) :].lstrip()
        # The item is already in the block's label; repeating it here would be
        # the one piece of duplication a reader would notice.
        if item and head.startswith(f"(item={item})"):
            head = head[len(f"(item={item})") :].lstrip()
            if head.startswith(_DUMP_SEP):
                head = head[len(_DUMP_SEP) :].lstrip()
        if not head:
            return rest
        return head + newline + rest

    def _tp_result(self, result, method: str, status: str, color, *args, **kwargs):
        """Run one of `default`'s result hooks and reframe what it printed."""
        self._tp_emit_header()
        frame: list[str] = []
        self._tp_frames.append(frame)
        try:
            getattr(super(), method)(result, *args, **kwargs)
        finally:
            self._tp_frames.pop()
        # An empty frame is `default` declining to speak -- `display_ok_hosts:
        # false`, a `TaskInclude`, a loop whose items reported themselves. It
        # decides whether there is anything to say; we decide how it looks.
        if not frame:
            return None

        host = self._tp_field(result, "host")
        name = host.get_name() if host is not None else None
        item = self._tp_item_of(result)
        label = self._tp_label_of(result)
        if item:
            label = f"{label} (item={item})"
        host_label = self._tp_host_label(result)

        body = []
        for chunk in frame:
            trimmed = self._tp_trim(chunk, host_label, item)
            if trimmed:
                body.append(trimmed)

        task = self._tp_field(result, "task")
        key = (getattr(task, "_uuid", None), name)
        started = self._tp_started.pop(key, self._tp_task_at)
        self._tp_block(
            host=name,
            status=status,
            label=label,
            body=body,
            color=color,
            stamp=self._tp_stamp(started),
        )
        return None

    @staticmethod
    def _tp_changed(result) -> bool:
        try:
            data = CallbackModule._tp_field(result, "result")
            return bool(data.get("changed", False))
        except Exception:  # noqa: BLE001
            return False

    def v2_runner_on_ok(self, result, *a, **k):
        changed = self._tp_changed(result)
        return self._tp_result(
            result,
            "v2_runner_on_ok",
            "changed" if changed else "ok",
            C.COLOR_CHANGED if changed else C.COLOR_OK,
            *a,
            **k,
        )

    def v2_runner_on_failed(self, result, *a, **k):
        return self._tp_result(result, "v2_runner_on_failed", "FAILED", C.COLOR_ERROR, *a, **k)

    def v2_runner_on_skipped(self, result, *a, **k):
        return self._tp_result(result, "v2_runner_on_skipped", "skipped", C.COLOR_SKIP, *a, **k)

    def v2_runner_on_unreachable(self, result, *a, **k):
        return self._tp_result(
            result, "v2_runner_on_unreachable", "UNREACHABLE", C.COLOR_UNREACHABLE, *a, **k
        )

    def v2_runner_item_on_ok(self, result, *a, **k):
        changed = self._tp_changed(result)
        return self._tp_result(
            result,
            "v2_runner_item_on_ok",
            "changed" if changed else "ok",
            C.COLOR_CHANGED if changed else C.COLOR_OK,
            *a,
            **k,
        )

    def v2_runner_item_on_failed(self, result, *a, **k):
        return self._tp_result(result, "v2_runner_item_on_failed", "FAILED", C.COLOR_ERROR, *a, **k)

    def v2_runner_item_on_skipped(self, result, *a, **k):
        return self._tp_result(
            result, "v2_runner_item_on_skipped", "skipped", C.COLOR_SKIP, *a, **k
        )

    def v2_runner_on_async_ok(self, result, *a, **k):
        return self._tp_result(result, "v2_runner_on_async_ok", "ok", C.COLOR_OK, *a, **k)

    def v2_runner_on_async_failed(self, result, *a, **k):
        return self._tp_result(
            result, "v2_runner_on_async_failed", "FAILED", C.COLOR_ERROR, *a, **k
        )

    def v2_runner_retry(self, result, *a, **k):
        return self._tp_result(result, "v2_runner_retry", "retry", C.COLOR_DEBUG, *a, **k)

    def v2_on_file_diff(self, result, *a, **k):
        """A `--diff` belongs to the host whose file changed, not to the play."""
        return self._tp_result(result, "v2_on_file_diff", "diff", C.COLOR_CHANGED, *a, **k)

    # -- timing -----------------------------------------------------------
    def v2_runner_on_start(self, host, task, *a, **k):
        """Where a block's duration comes from, rather than `profile_tasks`.

        Per host AND per task, so the figure is this host's time on this task
        and stays right under `strategy: free`, where several tasks are in
        flight at once and a single "when the task started" would not be.
        """
        try:
            self._tp_started[(getattr(task, "_uuid", None), host.get_name())] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass
        self._tp_emit_header()
        frame: list[str] = []
        self._tp_frames.append(frame)
        try:
            super().v2_runner_on_start(host, task, *a, **k)
        finally:
            self._tp_frames.pop()
        if not frame:
            return None
        # Only reached with `show_per_host_start`, which is off by default.
        try:
            name = host.get_name()
        except Exception:  # noqa: BLE001
            name = None
        self._tp_block(
            host=name,
            status="start",
            label=self._tp_task_label(task),
            body=frame,
            color=C.COLOR_OK,
            stamp=self._tp_stamp(),
        )
        return None

    @staticmethod
    def _tp_task_label(task) -> str:
        try:
            return task.get_name().strip()
        except Exception:  # noqa: BLE001
            return ""

    # -- play-level output ------------------------------------------------
    #
    # The PLAY banner and the PLAY RECAP are context: in a filtered view they
    # are what tells the reader which play a block belongs to and how the run
    # ended, so they are host-less BLOCKS rather than dropped.
    def v2_playbook_on_start(self, playbook):
        # The earliest hook, so the header precedes as much of our own output
        # as possible. It cannot be first in the FILE -- galaxy output and the
        # orchestrator's structlog lines are rolled into the same artifact
        # around it -- which is why the parser searches for it.
        self._tp_emit_header()
        return super().v2_playbook_on_start(playbook)

    def v2_playbook_on_play_start(self, play):
        self._tp_emit_header()
        return super().v2_playbook_on_play_start(play)

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._tp_emit_header()
        self._tp_task_at = time.monotonic()
        return super().v2_playbook_on_task_start(task, is_conditional)

    def v2_playbook_on_handler_task_start(self, task):
        self._tp_emit_header()
        self._tp_task_at = time.monotonic()
        return super().v2_playbook_on_handler_task_start(task)

    def v2_playbook_on_stats(self, stats):
        """The recap, as one host-less block: its banner plus every summary row.

        Framed so the rows land in the block rather than trailing after it --
        the banner itself is dropped by the frame rule and reinstated as the
        block's own label, which is why this does not go through `_tp_banner`.
        """
        self._tp_emit_header()
        frame: list[str] = []
        self._tp_frames.append(frame)
        try:
            super().v2_playbook_on_stats(stats)
        finally:
            self._tp_frames.pop()
        self._tp_block(
            host=None,
            status="recap",
            label="PLAY RECAP",
            body=[line for line in frame if line.strip()],
            color=None,
            stamp=self._tp_stamp(),
        )
