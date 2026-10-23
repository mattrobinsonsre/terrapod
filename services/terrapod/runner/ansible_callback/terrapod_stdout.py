"""Tag every line ansible prints with the host it belongs to (#2144).

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

## Why a callback rather than parsing the combined stdout

Because the combined stream cannot be attributed, measured on ansible-core
2.21.5 over a 3-host fixture: only **12 of 41** non-blank lines carry `[host]`
at all (6 of 24 under `strategy: free`). Three classes defeat a text parser
outright, and they are exactly the lines someone filters in order to debug:

* the `[ERROR]:` block, its `Origin:` path and the quoted YAML excerpt are
  emitted BEFORE the `fatal: [web2]` line that names the host;
* `[WARNING]` lines name the host in prose, in single quotes, out of order;
* `...ignoring` after an ignored failure names no host.

A callback holds the host structurally for all of them, because it is handed the
result object.

## The format

A self-identifying header, then one prefix per line:

    #terrapod-host-log v1
    web1       | ok: [web1] => {
    web1       |     "msg": "hello from web1"
    web1       | }
    ~          | TASK [say hello] ******************************************

**The header is NOT at offset 0 of the stored log**, and a parser must not
assume it is. `ansible_collections.install` writes galaxy output to the same
file first, and the orchestrator's own structlog lines are rolled in around it,
so the file is legitimately part untagged. Everything before the header is
host-less context.

`~` is the host-less marker -- task banners, the PLAY RECAP, play-level output.
It is explicit rather than "no prefix" because the absence of a prefix is also
what an older runner's log looks like, and a viewer must not confuse "this line
belongs to every host" with "this log has no tagging".

The marker cannot collide with a host name because `~` is in the escape set:
any host actually called `~` renders as `%7E`. Escaping rather than validating
is forced -- `validate_host_name` refuses whitespace and `--limit`'s operators
but not `|`, and a host arriving from a **git** inventory source never passes
through it at all, so the format can assume nothing about a host name.

The tag is removable without touching ansible's own words, which is what lets
the traditional view show them exactly: the parser splits on the first
delimiter and the remainder is byte-identical to what ansible rendered.
"""

from __future__ import annotations

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
    short_description: ansible's default output, with every line tagged by host
    description:
        - Renders exactly as the `default` callback does, then prefixes each
          emitted line with the host it belongs to so a viewer can show one
          host's output alone (#2144).
        - Lines belonging to no single host -- task banners, the PLAY RECAP --
          carry a reserved marker instead, because they are context a filtered
          view still has to show.
    extends_documentation_fragment:
      - default_callback
      - result_format_callback
"""

#: Emitted once, before anything this plugin tags. A viewer keys the per-host
#: switch on finding it: an older runner image emits ansible's plain output, so
#: its absence means "traditional view only", which is the data-gating rule
#: applied to a log rather than to a column.
HEADER = "#terrapod-host-log v1"

#: Separates the host field from ansible's own line. The spaces are part of it
#: so the common case reads like `docker compose` and `kubectl logs --prefix`,
#: which is the shape operators already know.
DELIMITER = " | "

#: Lines that belong to no single host.
HOSTLESS = "~"

#: Host field width, for readability alone. A longer name simply widens its own
#: line; nothing parses by column.
_PAD = 10

#: What must never appear raw in the host field. `%` is first, or escaping it
#: afterwards would double-escape the sequences introduced by the others.
_ESCAPE = ("%", "|", "~")


def escape_host(host: str) -> str:
    """Make `host` safe for the host field, reversibly.

    Percent-escaping, so the result is still legible for an ordinary name (the
    overwhelming case is that nothing is escaped at all) and unambiguous for
    anything else.
    """
    out = host
    for ch in _ESCAPE:
        out = out.replace(ch, f"%{ord(ch):02X}")
    # Every whitespace character, not just the space: a tab or a newline inside
    # a host name would otherwise break the line discipline the format rests on.
    return "".join(c if not c.isspace() else f"%{ord(c):02X}" for c in out)


class CallbackModule(DefaultCallback):
    """`default`'s rendering, with every line tagged. Never raises into ansible."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "terrapod_stdout"

    def __init__(self) -> None:
        super().__init__()
        self._tp_host: str | None = None
        self._tp_wrapped = False
        self._tp_header_done = False

    # -- the interception -------------------------------------------------
    #
    # `default` renders through `self._display.display(...)`. Wrapping that one
    # method tags every line it emits, including the continuation lines of a
    # multi-line result -- which is the whole reason this is a callback and not
    # a parser, since those lines carry no host of their own.
    #
    # The `Display` object is a process-wide singleton, so the wrapper is
    # installed on OUR instance attribute rather than on the class: ansible's
    # own `Display.warning` does not go through a callback's display instance
    # (measured), and reaching for the class to catch it would also tag output
    # from anything else holding the singleton.
    def _tp_wrap_display(self) -> None:
        if self._tp_wrapped:
            return
        self._tp_wrapped = True
        inner = self._display.display

        def tagged(msg="", *args, **kwargs):
            try:
                msg = self._tp_tag(msg)
            except Exception:  # noqa: BLE001 - never fail a play to format a log
                pass
            return inner(msg, *args, **kwargs)

        self._display.display = tagged  # type: ignore[method-assign]

    def _tp_tag(self, msg) -> str:
        """Prefix every line of `msg`. Returns it unchanged on anything odd."""
        if not isinstance(msg, str):
            return msg
        host = escape_host(self._tp_host) if self._tp_host else HOSTLESS
        prefix = host.ljust(_PAD) + DELIMITER
        # `splitlines` would drop the information that a message ended with a
        # newline, so split on "\n" -- but a message that ENDS with one yields a
        # final empty field, and prefixing that invents a line ansible never
        # printed. Measured on core 2.21.5, whose `[ERROR]` block ends with a
        # newline: the traditional view gained one blank line per such call,
        # which is exactly the byte-identity the subclass exists to preserve.
        # (The comment here previously claimed the empty field "round-trips as
        # the blank line ansible intended". It does not -- it adds one.)
        lines = msg.split("\n")
        trailing = len(lines) > 1 and lines[-1] == ""
        if trailing:
            lines = lines[:-1]
        tagged = "\n".join(prefix + line for line in lines)
        return tagged + "\n" if trailing else tagged

    def _tp_emit_header(self) -> None:
        if self._tp_header_done:
            return
        self._tp_header_done = True
        # Before the wrapper, so the header itself is untagged -- it describes
        # the format rather than being an instance of it.
        try:
            self._display.display(HEADER)
        except Exception:  # noqa: BLE001
            pass
        self._tp_wrap_display()

    # -- host attribution -------------------------------------------------
    #
    # Each `v2_runner_on_*` hook is handed the result, which carries the host.
    # Setting it around the `super()` call is what gives the continuation lines
    # their attribution: they are emitted from inside that call.
    def _tp_for(self, result, method: str, *args, **kwargs):
        self._tp_emit_header()
        try:
            self._tp_host = result._host.get_name()
        except Exception:  # noqa: BLE001 - an unnamed host is host-less, not fatal
            self._tp_host = None
        try:
            return getattr(super(), method)(result, *args, **kwargs)
        finally:
            self._tp_host = None

    def v2_runner_on_ok(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_on_ok", *a, **k)

    def v2_runner_on_failed(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_on_failed", *a, **k)

    def v2_runner_on_skipped(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_on_skipped", *a, **k)

    def v2_runner_on_unreachable(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_on_unreachable", *a, **k)

    def v2_runner_item_on_ok(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_item_on_ok", *a, **k)

    def v2_runner_item_on_failed(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_item_on_failed", *a, **k)

    def v2_runner_item_on_skipped(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_item_on_skipped", *a, **k)

    def v2_runner_on_async_ok(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_on_async_ok", *a, **k)

    def v2_runner_on_async_failed(self, result, *a, **k):
        return self._tp_for(result, "v2_runner_on_async_failed", *a, **k)

    # -- everything else is host-less, and must still be emitted ----------
    #
    # A task banner and the PLAY RECAP are context: in a filtered view they are
    # what tells the reader WHICH task a line belongs to, so they carry the
    # host-less marker rather than being dropped.
    def v2_playbook_on_start(self, playbook):
        # The earliest hook, so the header precedes as much of our own tagged
        # output as possible. It cannot be first in the FILE -- galaxy output
        # and the orchestrator's structlog lines are rolled into the same
        # artifact around it -- which is why the parser searches for it.
        self._tp_emit_header()
        return super().v2_playbook_on_start(playbook)

    def v2_playbook_on_play_start(self, play):
        self._tp_emit_header()
        return super().v2_playbook_on_play_start(play)

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._tp_emit_header()
        return super().v2_playbook_on_task_start(task, is_conditional)

    def v2_playbook_on_stats(self, stats):
        self._tp_emit_header()
        return super().v2_playbook_on_stats(stats)
