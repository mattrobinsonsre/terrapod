"""Why a runner phase failed, in a line or two, for the run's error message (#1631).

A failed run used to say only "Runner exited with code 1": the runner knew the
cause, but the only thing it told the API was its exit code. This module works
the cause out so the end-of-run resource-profile POST can carry it:

- a tofu/terraform init, plan or apply that failed: tofu's own ``Error:``
  summaries from that phase's log, each with its ``on <file> line <n>``
  location;
- any other failure: the runner's own fatal error (a failed hook, an unusable
  configuration archive, a crash), from an allowlist of the events that end a
  run.

Only diagnostic summaries are taken — never a diagnostic's detail lines, which
can quote values — and the result is bounded. The message lands in the run's
error_message, readable by anyone with run-read, so free text is forwarded only
from events that are the runner's own account of what went wrong; an
unexpected exception's message stays in the log. Best-effort throughout:
nothing here raises.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

MAX_REASON_CHARS = 1500
_MAX_TOFU_ERRORS = 3
# Diagnostics come at the end of a failed command's output; a bounded tail
# keeps a large plan log from being read whole.
_TAIL_BYTES = 256 * 1024

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# tofu frames each diagnostic in box-drawing characters: "╷", "│ Error: …", "╵".
_FRAME = re.compile(r"^[\s│╷╵]+")
_ERROR = re.compile(r"^Error:\s*(\S.*)$")
_LOCATION = re.compile(r"^on (.+? line \d+)")

_ERROR_LEVELS = frozenset({"error", "exception", "critical"})

# The runner's fatal errors, matched on the start of the log event, and the
# fields of each that are safe to show. An error logged on a path that carries
# on (a missing plan-artifacts tarball, say) is not here, so it can never be
# reported as the cause of a failure that happened later.
_FATAL_EVENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pre_init hook failed", ("hook", "rc")),
    ("pre_plan hook failed", ("hook", "rc")),
    ("post_plan hook failed", ("hook", "rc")),
    ("pre_apply hook failed", ("hook", "rc")),
    ("post_apply hook failed", ("hook", "rc")),
    ("init failed", ("rc",)),
    ("binary download failed", ("err",)),
    ("configuration archive unusable", ("err",)),
    ("failed to render terraform variables", ("error",)),
    ("backend backstop failed", ("err",)),
    ("policy evaluation failed", ("err",)),
    ("state upload raised", ("err",)),
    ("unknown phase", ("phase",)),
    # An unexpected exception's text could be anything; the traceback is in the log.
    ("orchestrator crashed", ()),
)
_CRASHED = "orchestrator crashed"

_last_error: dict[str, Any] | None = None


def remember_errors(_logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: keep the most recent fatal runner error."""
    global _last_error
    if method_name in _ERROR_LEVELS:
        event = str(event_dict.get("event", ""))
        for label, fields in _FATAL_EVENTS:
            if event.startswith(label):
                _last_error = {"label": label}
                _last_error.update(
                    {k: event_dict[k] for k in fields if event_dict.get(k) not in (None, "")}
                )
                break
    return event_dict


def reset() -> None:
    """Forget the remembered error (tests)."""
    global _last_error
    _last_error = None


def last_logged_error() -> str | None:
    """The runner's most recent fatal error as one line, or None."""
    if not _last_error:
        return None
    label = _last_error["label"]
    if label == _CRASHED:
        return f"{_CRASHED} — see the run log for the traceback"
    extras = ", ".join(f"{k}={_last_error[k]}" for k in ("hook", "rc", "phase") if k in _last_error)
    text = f"{label} ({extras})" if extras else label
    detail = _last_error.get("err") or _last_error.get("error")
    if detail:
        text += f": {detail}"
    return text


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - _TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def tofu_errors(log_path: Path) -> list[str]:
    """tofu's ``Error:`` diagnostics in ``log_path``, at most three, each as
    ``Error: <summary> (on <file> line <n>)`` when tofu gave a location."""
    lines = [_FRAME.sub("", _ANSI.sub("", ln)).strip() for ln in _tail(log_path).splitlines()]
    found: list[str] = []
    for i, line in enumerate(lines):
        match = _ERROR.match(line)
        if not match:
            continue
        summary = f"Error: {match.group(1).strip()}"
        for following in lines[i + 1 : i + 6]:
            if _ERROR.match(following):
                break
            location = _LOCATION.match(following)
            if location:
                summary += f" (on {location.group(1)})"
                break
        if summary not in found:
            found.append(summary)
        if len(found) == _MAX_TOFU_ERRORS:
            break
    return found


def _bound(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_REASON_CHARS:
        return text
    return text[: MAX_REASON_CHARS - 1] + "…"


def failure_reason(exit_code: int, phase_logs: list[Path]) -> str | None:
    """Why the run failed, or None for a clean exit or an unknown cause.

    ``phase_logs`` are the tofu phase logs, the latest phase first, so a plan
    that failed after a clean init reports the plan's errors.
    """
    if exit_code == 0:
        return None
    try:
        for log in phase_logs:
            errors = tofu_errors(log)
            if errors:
                return _bound("\n".join(errors))
        last = last_logged_error()
        return _bound(last) if last else None
    except Exception:  # noqa: BLE001 — best-effort; the exit code still reports the failure
        return None
