"""Hold a failed runner pod open so an operator can get inside it (#1764).

A runner Job's pod runs `restartPolicy: Never`, so the moment the orchestrator
exits non-zero the container is *terminated* — and you cannot `kubectl exec`
into a terminated container at any TTL. That is exactly when an operator most
wants to look: at the credentials that did not work, the DNS or `hostAliases`
that did not resolve, the mounts that were not there, the egress that was
blocked. Raising `ttlSecondsAfterFinished` does not help, because the problem is
not how long the Job is kept but that the process is already gone.

So when the workspace has debug mode on, the orchestrator reports the failure to
the API exactly as it always does, and only *then* holds the container open
instead of exiting. The run is failed, visible and final from Terrapod's side;
the pod is simply still there.

**This deliberately does not try to catch an OOM.** An OOMKill is a SIGKILL from
the kernel — nothing in this process gets to run, so nothing here could hold the
container. It also does not need to: the run already records
`runner_exit_reason` and `peak_memory_bytes`, so an OOM is answerable from the
run page without a shell. The failures this serves all exit non-zero.

**The linger is bounded twice, deliberately.** This module sleeps for at most
what it is told, and the Job carries an `activeDeadlineSeconds` that already
covers the run plus that window — so if this process misbehaves, the cluster
ends the pod anyway. The pod holds the run's auth token and its decrypted
`terraform.tfvars.json`, so the ceiling is a safety property, not a convenience.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType

import structlog

# `structlog` directly, not `terrapod.logging_config`: the runner image ships
# only the modules Dockerfile.runner names, and that one is not among them --
# importing it would raise ModuleNotFoundError inside every runner Job while
# every test on a full checkout passed. Matches `plan_artifacts` and
# `lock_extender`, which log the same way for the same reason.
log = structlog.get_logger("runner.debug_linger")

#: Set by `job_template` from the deployment's configured window when the
#: workspace has debug mode on. Absent or "0" means the normal behaviour.
ENV_VAR = "TP_DEBUG_LINGER_SECONDS"


def _configured_seconds() -> int:
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        # A malformed value must not change whether the run reports its
        # failure -- it has already done so by the time we are called.
        log.warning("ignoring malformed debug linger window", value=raw)
        return 0


def hold_for_inspection(exit_code: int, *, sleep: object = None) -> bool:
    """Hold the container open after a failure. Returns whether it held.

    A no-op on success and whenever debug mode is off, so the ordinary path is
    unchanged. `sleep` is injectable so the tests do not actually wait.
    """
    if exit_code == 0:
        return False
    seconds = _configured_seconds()
    if seconds <= 0:
        return False

    released = threading.Event()

    def _release(signum: int, _frame: FrameType | None) -> None:
        # `kubectl delete pod` should be immediate rather than waiting out the
        # window -- an operator who is finished looking says so by deleting it.
        log.info("debug linger released by signal", signal=signum)
        released.set()

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _release)
        except ValueError:
            # Not on the main thread; the window still applies, it just
            # cannot be cut short. Never fatal.
            pass

    log.warning(
        "holding this pod open for inspection — debug mode is on for this "
        "workspace. The run has already been reported as failed. The pod will "
        "be removed when the window expires or when you delete it.",
        seconds=seconds,
        exit_code=exit_code,
    )
    # Say it on stdout too: the operator reads the run log, not the
    # structured logger's sink.
    print(
        f"\n=== debug mode: holding this pod for up to {seconds}s so you can "
        f"exec into it. The run has already failed with exit {exit_code}. ===",
        flush=True,
    )
    sys.stdout.flush()

    try:
        if sleep is not None:
            sleep(seconds)  # type: ignore[operator]
        else:
            # `Event.wait` rather than `time.sleep`, so a signal cuts it short.
            released.wait(timeout=seconds)
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass

    log.info("debug linger over", seconds=seconds)
    return True


__all__ = ["ENV_VAR", "hold_for_inspection"]
