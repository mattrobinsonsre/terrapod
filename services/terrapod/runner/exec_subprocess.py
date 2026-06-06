"""Subprocess execution with signal forwarding + watchdog timer.

Port of the SIGINT-forwarding + SIGKILL-watchdog logic in
docker/runner-entrypoint.sh. Run as `python -m
terrapod.runner.exec_subprocess --log-file /tmp/plan.log -- <cmd...>`.

Behaviour (bash parity):

  * On SIGTERM (the kubelet's default termination signal) we forward
    a SIGINT to the child. Why SIGINT and not SIGTERM? HashiCorp's
    recommendation for terraform / tofu in containers is SIGINT: it
    triggers the graceful path that finishes the current API call,
    writes state, releases the workspace lock, and exits. SIGTERM is
    handled but the docs put SIGINT first.

  * Only ONE signal is sent to the child. terraform treats a second
    INT/TERM as ungraceful — it aborts immediately and may skip state
    writing. After the first SIGINT we start a watchdog timer; when
    `child_grace_seconds` elapses we send SIGKILL to the child.

  * Stdout + stderr are merged into the configured log file. The
    child's combined output is also tee'd to our own stdout so the
    listener picks it up via `kubectl logs` for the live UI stream.

  * Returns the child's exit code. If the watchdog had to escalate to
    SIGKILL the exit code is whatever the kernel reported — usually
    137 (128 + SIGKILL) on Linux, sometimes 143 on a clean SIGTERM
    that the child handled itself. Callers don't need to interpret;
    bash's `EXIT_CODE` just inherits.

Bash invokes us once per phase (plan, apply). The CHILD_GRACE budget
is `TP_TERMINATION_GRACE - TP_UPLOAD_TIMEOUT - SAFETY_MARGIN` so the
artifact upload phase still has time to run after a graceful child
exit — same formula bash uses.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import structlog

logger = structlog.get_logger("runner.exec_subprocess")


@dataclass
class ExecResult:
    exit_code: int
    signalled: bool
    killed_by_watchdog: bool


class _SignalState:
    """Mutable state shared between the main thread and the SIGTERM
    handler. Held in a class because Python's signal-handler runs on
    the main thread and can't take closures cleanly across threads."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.signalled: bool = False
        self.child_grace_seconds: float = 25.0


def _install_handler(state: _SignalState) -> None:
    """Forward SIGTERM/SIGQUIT to the child as SIGINT, exactly once,
    then start a watchdog to SIGKILL the child after the grace
    window."""

    def handler(signum: int, _frame) -> None:  # noqa: ARG001
        if state.proc is None:
            return
        if state.signalled:
            # Don't double-signal. Bash had the same guard via the trap
            # being a one-shot. A second forwarded INT to terraform =
            # ungraceful abort.
            return
        state.signalled = True
        logger.warning(
            "received signal — forwarding SIGINT to child",
            received=signum,
            child_pid=state.proc.pid,
            grace_seconds=state.child_grace_seconds,
        )
        try:
            state.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            # Child already exited between signal arrival and our send.
            return

        # Watchdog: SIGKILL the child after grace_seconds. Daemon so a
        # natural child exit lets the process unwind without joining.
        def _watchdog() -> None:
            time.sleep(state.child_grace_seconds)
            if state.proc is None:
                return
            if state.proc.poll() is None:
                logger.error(
                    "grace expired — sending SIGKILL",
                    child_pid=state.proc.pid,
                    grace_seconds=state.child_grace_seconds,
                )
                try:
                    state.proc.send_signal(signal.SIGKILL)
                except ProcessLookupError:
                    pass

        t = threading.Thread(target=_watchdog, daemon=True, name="exec-watchdog")
        t.start()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGQUIT, handler)


def run(
    argv: list[str],
    *,
    log_file: str | None = None,
    child_grace_seconds: float = 25.0,
    tee_to_stdout: bool = True,
) -> ExecResult:
    """Run `argv` as a child process with signal forwarding.

    Args:
        argv: program + arguments. argv[0] should be an absolute path
            or already on PATH.
        log_file: if set, child's combined stdout/stderr is written
            here. Created if missing, truncated if present.
        child_grace_seconds: how long to wait after forwarding SIGINT
            before escalating to SIGKILL. Defaults to 25s — long
            enough for terraform's normal state-write cycle.
        tee_to_stdout: also write child output to our stdout so the
            kubelet log stream sees it.

    Returns ExecResult with the child's exit code.
    """
    state = _SignalState()
    state.child_grace_seconds = child_grace_seconds
    _install_handler(state)

    log_fh = None
    if log_file:
        log_fh = open(log_file, "wb", buffering=0)  # noqa: SIM115 — closed below

    proc = subprocess.Popen(  # noqa: S603 — argv is operator-supplied
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    state.proc = proc

    try:
        assert proc.stdout is not None
        # Read child output a chunk at a time. Larger chunks reduce
        # syscall overhead vs. readline().
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            if log_fh is not None:
                log_fh.write(chunk)
            if tee_to_stdout:
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    pass
    finally:
        if log_fh is not None:
            log_fh.close()

    rc = proc.wait()
    killed_by_watchdog = rc == -signal.SIGKILL or rc == 128 + signal.SIGKILL

    return ExecResult(
        exit_code=rc if rc >= 0 else 128 - rc,  # convert negative-rc to shell convention
        signalled=state.signalled,
        killed_by_watchdog=killed_by_watchdog,
    )


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="python -m terrapod.runner.exec_subprocess",
        description="Run a child with SIGTERM-to-SIGINT forwarding + SIGKILL watchdog.",
    )
    parser.add_argument(
        "--log-file",
        help="Write combined stdout+stderr to this file in addition to teeing.",
    )
    parser.add_argument(
        "--child-grace-seconds",
        type=float,
        default=25.0,
        help="Seconds between forwarded SIGINT and escalation SIGKILL.",
    )
    parser.add_argument(
        "--no-tee",
        action="store_true",
        help="Don't tee child output to our stdout (useful for tests).",
    )
    # Everything after `--` is the child argv.
    if "--" in argv:
        ix = argv.index("--")
        own_args = argv[:ix]
        child_argv = argv[ix + 1 :]
    else:
        own_args = argv
        child_argv = []
    ns = parser.parse_args(own_args)
    return ns, child_argv


def main(argv: list[str] | None = None) -> int:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        cache_logger_on_first_use=True,
    )
    if argv is None:
        argv = sys.argv[1:]
    ns, child_argv = _parse_args(argv)
    if not child_argv:
        print(
            "exec_subprocess: missing child command (expected `... -- CMD [ARGS...]`)",
            file=sys.stderr,
        )
        return 2

    try:
        result = run(
            child_argv,
            log_file=ns.log_file,
            child_grace_seconds=ns.child_grace_seconds,
            tee_to_stdout=not ns.no_tee,
        )
    except FileNotFoundError as exc:
        # Surface a clearer message than the bare OSError.
        print(f"exec_subprocess: failed to launch {child_argv[0]}: {exc}", file=sys.stderr)
        return 127
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
