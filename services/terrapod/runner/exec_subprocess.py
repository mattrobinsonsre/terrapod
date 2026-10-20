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
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import structlog

from terrapod.runner.reserved_env import is_reserved_env_key

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


#: Values this run holds that must never appear in its log stream.
#:
#: **This is the one place redaction is the right tool, and it is worth saying why,
#: because the house rule is the opposite.** `git_auth` states it: "the runner
#: streams stdout/stderr verbatim, so the guarantee is mechanism, not redaction" --
#: credentials live in 0600 files read out-of-band, never in an argv or on stdout.
#: That holds for every secret the runner controls, and it is a better guarantee
#: than scrubbing.
#:
#: A cloud identity token is the exception, because we hand it to third-party code
#: ON PURPOSE. The token is in a 0600 file and scrubbed from the engine's
#: environment, so nothing on our side prints it -- and then the provider reads the
#: file and puts it in its OWN output. Measured: terraform-provider-aws logs the
#: STS `AssumeRoleWithWebIdentity` request body, which carries
#: `WebIdentityToken=<the whole JWT>`, at `TF_LOG=DEBUG` and above. Its only body
#: redaction masks AWS access keys and a run of exactly 40 base64 characters, and a
#: base64url JWT matches neither. (azurerm, google and vault do not; see #2047.)
#:
#: No mechanism of ours can prevent that, so the token is live in the run log for
#: `token_ttl_seconds` and readable by anyone with workspace read.
#:
#: **Exact values, never a JWT-shaped pattern.** The runner knows the literal
#: strings -- it wrote them -- so this cannot false-positive on a plan that
#: legitimately contains a JWT (a Kubernetes Secret, an OIDC config resource), and
#: it cannot miss the token it was handed. A shape regex would be wrong in both
#: directions.
#:
#: A module global rather than a parameter threaded through `init_phase` and
#: `plan_apply`, which have no other interest in it: a runner Job is one
#: single-purpose process, and "the secrets this run holds" is genuinely
#: process-wide. Not the in-process state AGENTS.md rule 11 forbids -- that is
#: about coordination between API replicas, and there is nothing here to
#: coordinate.
_SECRETS: set[bytes] = set()


def _scan_ceiling(secrets: tuple[bytes, ...]) -> int:
    """The scan window, and with it the bound on the carry.

    `longest secret - 1`: nothing longer than that can be a *proper* prefix of a
    secret, so nothing longer ever needs holding. One function rather than an
    expression in the loop, because the tests need the same number and a second
    copy of the rule is how the two come to disagree.
    """
    return max((len(secret) for secret in secrets), default=1) - 1


def _partial_secret_suffix(buf: bytes, *, secrets: tuple[bytes, ...], ceiling: int) -> int:
    """How many trailing bytes of `buf` could still be the start of a secret.

    Zero for almost all real output, which is the whole point: the only bytes
    worth holding are the ones that might be a secret half-arrived.

    Scanned from the left of the window, so the FIRST position that matches
    gives the longest suffix and the search can stop there. `ceiling` bounds the
    window at `longest secret - 1`, since nothing longer than that can be a
    proper prefix.

    The first-byte test is what keeps this cheap: only an offset whose byte
    opens some secret can begin a partial match, and across a 5 MB log that was
    1.33% of bytes.
    """
    if not secrets:
        return 0
    window = buf[-ceiling:] if 0 < ceiling < len(buf) else buf
    base = len(buf) - len(window)
    first_bytes = {secret[:1] for secret in secrets}
    for i in range(len(window)):
        if window[i : i + 1] not in first_bytes:
            continue
        tail = buf[base + i :]
        for secret in secrets:
            # Strictly longer: an occurrence of the whole secret cannot be here,
            # because `_redact` ran over this buffer before we were called.
            if len(secret) > len(tail) and secret.startswith(tail):
                return len(tail)
    return 0


def _split_tail(buf: bytes, *, secrets: tuple[bytes, ...], ceiling: int) -> tuple[bytes, bytes]:
    """Split already-redacted bytes into (emit now, hold back).

    A secret can STRADDLE two reads, so something must be held back or a
    per-chunk replace misses exactly that case -- silently, while looking as
    though it worked.

    **What is held back is only what could be a secret in the making** (#2145).
    An earlier version held `longest secret - 1` BYTES, which made the straddle
    impossible and was badly behaved in TIME: `bufsize=0` makes a read one
    syscall returning whatever is available, typically a single line, so a
    stream emitting one short line every ten seconds sat silent for minutes.
    That was fixed by cutting at the last newline -- but only when no registered
    secret contained one, so registering anything multi-line (an SSH machine
    credential, #1933) took the whole run back to the byte bound and, measured,
    emitted nothing at all for two minutes.

    Holding the longest suffix that is a proper prefix of a secret needs no
    assumption about newlines, so there is no condition left to fall off:
    ordinary output holds back nothing, and a secret arriving in pieces holds
    back exactly its arrived-so-far bytes until it is whole and redacted.

    `ceiling` still bounds the scan window, and with it the carry, so a single
    line far longer than any secret cannot grow it without limit.
    """
    hold = _partial_secret_suffix(buf, secrets=secrets, ceiling=ceiling)
    cut = len(buf) - hold
    if cut <= 0:
        return b"", buf
    return buf[:cut], buf[cut:]


#: Below this, a value is SKIPPED rather than registered. A short string would
#: match ordinary output and mangle the log, which is a worse failure than the
#: leak it would prevent.
#:
#: Skipped and warned rather than raised, which it was first. Raising put the
#: power to FAIL A RUN in a log-hygiene mechanism: the only caller hands over
#: whatever the API returned, so a short or malformed token would have aborted a
#: run that was otherwise fine -- a security measure breaking legitimate
#: function, which is the one thing it must not do. Nothing of value is lost by
#: skipping, because a value this short cannot be a JWT and so is not the
#: credential this exists to hide.
_MIN_SECRET_LEN = 24

_REDACTED = b"[redacted: cloud identity token]"


def register_secret(value: str) -> bool:
    """Register a value to strip from the child's log stream. Idempotent.

    Returns whether it was registered. Best-effort by construction: see
    `_MIN_SECRET_LEN` for why a short value is skipped rather than refused.
    """
    if not value or len(value) < _MIN_SECRET_LEN:
        logger.warning(
            "not registering a redaction value below the minimum length; it would "
            "match ordinary output and mangle the log",
            length=len(value or ""),
            minimum=_MIN_SECRET_LEN,
        )
        return False
    _SECRETS.add(value.encode())
    return True


def _reset_secrets_for_tests() -> None:
    _SECRETS.clear()


def _redact(buf: bytes) -> bytes:
    for secret in _SECRETS:
        buf = buf.replace(secret, _REDACTED)
    return buf


class StreamRedactor:
    """Redact a byte stream chunk by chunk, holding back a possible partial secret.

    One producer for the straddle logic, because the bytes now arrive from two
    places: a child this module starts, and the execution-environment agent's
    streamed HTTP body (#1972, #2154). A second copy of the carry/ceiling
    reasoning is a second thing to get wrong, and it fails silently -- a secret
    split across two chunks passes through unredacted and the log looks fine.

    How much of the tail to hold back between reads: a secret can STRADDLE a
    chunk boundary, and a per-chunk replace would miss exactly that case. So the
    tail holds back only the longest suffix that could be the START of a secret
    (#2145), which for ordinary output is nothing at all.

    Bytes are the wrong unit for this. With `bufsize=0` a `read()` is one syscall
    returning whatever is available, typically a single line, so holding a fixed
    `longest - 1` bytes meant a `tofu apply` emitting one ~50-byte "Still
    creating..." line every ten seconds went quiet for nearly three minutes --
    the log someone watches to find where a slow apply is stuck. An earlier fix
    cut at the last newline instead, but only while no registered secret
    contained one, so registering a multi-line secret (an SSH machine credential,
    #1933) reverted the whole run to the byte bound: measured, nothing emitted at
    all for two minutes. Matching on prefixes needs no newline assumption, so
    that condition is gone rather than widened.

    `ceiling` remains as the scan window and the bound on the carry, so one line
    far longer than any secret cannot grow it without limit. With no secrets
    registered -- every run on a deployment not using per-workspace cloud
    identity -- `feed` returns its chunk unexamined.
    """

    def __init__(self) -> None:
        self._carry = b""

    def feed(self, chunk: bytes) -> bytes:
        """The bytes safe to emit now, holding back a possible secret prefix."""
        if not _SECRETS:
            return chunk
        buf = _redact(self._carry + chunk)
        # Read live, per chunk, rather than snapshotted once: `_redact` above
        # reads `_SECRETS` live, so a snapshot here could redact a secret whose
        # PREFIX the split does not know to hold -- a straddle hole for exactly
        # that secret. Two reads of the same state or neither; a tuple and a max
        # over at most a handful of values is nothing against a 4 KB read.
        secrets = tuple(_SECRETS)
        send, self._carry = _split_tail(buf, secrets=secrets, ceiling=_scan_ceiling(secrets))
        return send

    def flush(self) -> bytes:
        """The held-back tail.

        Emitted as-is and NOT re-redacted: the carry is only ever a slice of
        `_redact(carry + chunk)`, so every byte in it has already been through
        the redactor.
        """
        tail, self._carry = self._carry, b""
        return tail


def run(
    argv: list[str],
    *,
    log_file: str | None = None,
    child_grace_seconds: float = 25.0,
    tee_to_stdout: bool = True,
    env: dict[str, str] | None = None,
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
        env: the child's environment *instead of* this process's, for a
            caller that must REMOVE a variable rather than only add one
            (`os.environ.update` cannot). The reserved-key filter below
            still applies to it, so this cannot be used to smuggle a
            `TP_*` value into a child.

    Returns ExecResult with the child's exit code.
    """
    state = _SignalState()
    state.child_grace_seconds = child_grace_seconds
    _install_handler(state)

    log_fh = None
    if log_file:
        log_fh = open(log_file, "wb", buffering=0)  # noqa: SIM115 — closed below

    # Scrub the platform's own plumbing out of the child's environment. The
    # orchestrator holds `TP_AUTH_TOKEN` — the run's runner token, which can
    # upload artifacts and state for this run — and without `env=` the engine
    # and every provider plugin it loads inherits it. A provider is third-party
    # code running against the operator's credentials by design; it has no
    # business also holding the token that writes this run's state.
    #
    # A SCRUB rather than an allowlist, deliberately: the engine legitimately
    # needs `PATH`, `HOME`, `TMPDIR`, every `TF_*`/`PULUMI_*`, the cloud
    # credential variables, and the proxy and CA-bundle variables that libraries
    # read from nowhere else. An allowlist would have to enumerate all of that
    # correctly and would break a deployment behind an egress proxy the first
    # time it missed one.
    source_env = os.environ if env is None else env
    child_env = {k: v for k, v in source_env.items() if not is_reserved_env_key(k)}

    proc = subprocess.Popen(  # noqa: S603 — argv is operator-supplied
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=child_env,
    )
    state.proc = proc

    try:
        assert proc.stdout is not None

        def _emit(data: bytes) -> None:
            if log_fh is not None:
                log_fh.write(data)
            if tee_to_stdout:
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    pass

        # Straddle handling lives in `StreamRedactor` -- one producer, because the
        # execution-environment agent's streamed body goes through the same logic
        # (#1972). Its docstring carries the reasoning and the two measurements
        # that shaped it.
        redactor = StreamRedactor()

        # Read child output a chunk at a time. Larger chunks reduce
        # syscall overhead vs. readline().
        while True:
            # Larger chunks reduce syscall overhead against readline().
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            send = redactor.feed(chunk)
            if send:
                _emit(send)
        if tail := redactor.flush():
            # The flush is load-bearing: without it the held-back tail is
            # dropped and the end of the log disappears. It is deliberately not
            # re-redacted -- see `StreamRedactor.flush`.
            _emit(tail)
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
            structlog.dev.ConsoleRenderer(colors=False),
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
