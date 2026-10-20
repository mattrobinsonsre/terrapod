"""Drive ansible inside the execution-environment container (#1972, on #2154).

A configure runs ansible from the EE's filesystem -- its interpreter, its
ansible-core, its collections -- while every decision stays here in the
orchestrator, which holds the Terrapod code. Containers in a pod share a network
namespace but not a filesystem, so this talks to `terrapod-ee-agent` over
loopback rather than exec-ing into the EE.

**This is the only way the runner may run ansible.** The orchestrator image has
no ansible and must not grow one: the whole point of #2154 is that an operator's
ansible-core line is a property of the EE image rather than of Terrapod's own
Python floor, which moves for unrelated reasons.

Three things here are not obvious and each is load-bearing:

* **The exit code comes from `GET /result`, not from the `X-Terrapod-RC`
  trailer.** No Python HTTP client exposes HTTP trailers -- measured against both
  `httpx` and the stdlib `http.client`, each of which receives the streamed body
  correctly and sees only the `Trailer:` announcement header, never the values.
  The agent sets both; Go clients read the trailer, we read the endpoint.

* **No read timeout.** A configure is quiet for long stretches -- ansible emits
  nothing between a slow task's start and its result -- so a read deadline would
  kill a healthy run that is merely waiting. The bound on a configure's length is
  the Job's `activeDeadlineSeconds` and the reconciler's stale clock, not a
  socket timeout here. Connect and write keep short deadlines, because failing to
  reach a sidecar in the same pod is immediate or wrong.

* **Unreachable fails CLOSED.** A configure that could not be started must never
  look like a configure that changed nothing: `rc=0` with an empty log reads as
  success to every caller above. So this raises, naming the endpoint.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from dataclasses import dataclass

import httpx
import structlog

from terrapod.runner.exec_subprocess import StreamRedactor

log = structlog.get_logger(__name__)

#: Both set on the orchestrator container by `job_template._add_execution_environment`.
#: Their absence means this pod has no EE, which is every plan and apply.
ENDPOINT_ENV = "TP_EE_ENDPOINT"
TOKEN_ENV = "TP_EE_TOKEN"

#: Short on purpose: an agent in the same pod answers immediately or is not there.
#: `read=None` is the deliberate half -- see the module docstring.
_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)


class ExecutionEnvironmentUnavailable(RuntimeError):
    """The EE agent could not be reached, or answered in a way we cannot use.

    Raised rather than returned, because every caller above treats a returned
    exit code as ansible's verdict. A transport failure that arrived as `rc=0`
    would report a configure that never ran as one that succeeded.
    """


@dataclass(frozen=True)
class EEResult:
    """One invocation's verdict, as the agent reports it.

    `error` is the AGENT's own error -- the EE has no such binary, a child could
    not be started -- and is empty when a non-zero `rc` is ansible's own verdict.
    The distinction is what lets a caller tell "the playbook failed" from "this
    execution environment cannot run playbooks".
    """

    bin: str
    rc: int
    error: str = ""


def configured() -> bool:
    """Whether this pod has an execution environment to drive."""
    return bool(os.environ.get(ENDPOINT_ENV)) and bool(os.environ.get(TOKEN_ENV))


def _endpoint() -> tuple[str, dict[str, str]]:
    base = os.environ.get(ENDPOINT_ENV, "")
    token = os.environ.get(TOKEN_ENV, "")
    if not base or not token:
        raise ExecutionEnvironmentUnavailable(
            f"this pod has no execution environment: {ENDPOINT_ENV} and {TOKEN_ENV} "
            "are set only on a Job built with one, so a configure cannot run here"
        )
    return base.rstrip("/"), {"Authorization": f"Bearer {token}"}


@contextlib.contextmanager
def _forward_termination_to_the_execution_environment(binary: str):
    """Turn a SIGTERM here into a cancellation there, for one invocation.

    #2154 scope item 12. `exec_subprocess.run` forwards a signal to the child it
    started; a configure has no local child -- ansible runs in another container,
    parented by the agent -- so the same duty lands here, and nothing else would
    do it. Without this a terminating pod leaves ansible running until the
    kubelet kills the whole pod, part way through a fleet, with no chance to stop
    cleanly between tasks.

    **Cancellation is an endpoint rather than a signal**, because the agent is
    ansible's parent and can signal it directly -- which is exactly why the Job
    does not set `shareProcessNamespace` and why there is no pid here to signal
    even if we wanted to.

    **Once, not twice**, mirroring `exec_subprocess`'s own guard: the agent
    forwards ONE SIGINT and a second makes ansible abort ungracefully, possibly
    skipping what it was part way through. The handler is restored on the way
    out, so this affects exactly the window where there is something to cancel.
    """
    state = {"sent": False}
    previous: dict[int, object] = {}

    def handler(signum: int, _frame) -> None:  # noqa: ARG001
        if state["sent"]:
            return
        state["sent"] = True
        log.warning(
            "received signal — asking the execution environment to cancel",
            received=signum,
            binary=binary,
        )
        # Best-effort by construction: the run may already have finished, and a
        # failure to cancel must not replace whatever the run was going to
        # report. `cancel` swallows its own transport errors for that reason.
        cancel()

    for sig in (signal.SIGTERM, signal.SIGQUIT):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not the main thread, or the platform refuses: the configure still
            # runs, it just cannot be asked to stop early.
            pass
    try:
        yield
    finally:
        for sig, original in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, original)  # type: ignore[arg-type]


def run(
    binary: str,
    args: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    log_file: str | None = None,
    tee_to_stdout: bool = True,
) -> EEResult:
    """Run an allowlisted ansible entry point in the EE and stream its output.

    Args:
        binary: an entry point the agent allows (`ansible-playbook`,
            `ansible-galaxy`, ...). A name, never a path: the agent resolves it
            with `LookPath` inside the EE, where our filesystem does not reach.
        args: the rest of argv. The playbook and the limit are ours to choose,
            which is what the agent is for.
        cwd: the child's working directory, which for a configure is the
            workspace's `config_directory` -- `group_vars` beside an inventory
            and relative paths in the operator's `ansible.cfg` both resolve
            against it and nowhere else (#1929).
        env: variables ADDED to the EE's own environment. The agent scrubs its
            own token before handing it over, so a playbook cannot replay it.
        log_file: appended with the combined output, for upload as the run log.
        tee_to_stdout: also write to our stdout, so the kubelet stream sees it.

    Raises:
        ExecutionEnvironmentUnavailable: the agent was unreachable, or answered
            without a usable verdict.
    """
    base, headers = _endpoint()
    payload = {
        "bin": binary,
        "args": list(args),
        # The agent's wire form is `KEY=VALUE`, matching `os.Environ()`.
        "env": [f"{k}={v}" for k, v in (env or {}).items()],
        "dir": cwd,
    }

    redactor = StreamRedactor()
    # Append rather than truncate: a configure may be the second thing writing
    # this log -- it is concatenated onto the apply's stream from the CLI's point
    # of view (#1988), so truncating would delete the apply the operator is
    # watching.
    handle = open(log_file, "ab") if log_file else None  # noqa: SIM115 - closed in finally

    def emit(chunk: bytes) -> None:
        if handle is not None:
            handle.write(chunk)
            handle.flush()
        if tee_to_stdout:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()

    try:
        with (
            httpx.Client(timeout=_TIMEOUT) as client,
            _forward_termination_to_the_execution_environment(binary),
        ):
            try:
                with client.stream(
                    "POST", f"{base}/exec", headers=headers, json=payload
                ) as response:
                    if response.status_code != 200:
                        # Deliberately NOT reading the body. `read=None` above
                        # means a peer that sends an unframed response -- no
                        # Content-Length, no terminating chunk -- would hang us
                        # for ever rather than erroring. The status is all this
                        # needs, and the agent's own refusals are framed anyway.
                        raise ExecutionEnvironmentUnavailable(
                            f"the execution-environment agent refused {binary} at {base}: "
                            f"HTTP {response.status_code}"
                        )
                    for chunk in response.iter_bytes():
                        if send := redactor.feed(chunk):
                            emit(send)
                if tail := redactor.flush():
                    emit(tail)
            except httpx.HTTPError as exc:
                raise ExecutionEnvironmentUnavailable(
                    f"could not stream {binary} from the execution-environment agent "
                    f"at {base}: {exc}"
                ) from exc

            # Only now, with the body at EOF: the agent records the verdict under
            # its mutex before `/exec` returns, so it is already there.
            try:
                verdict = client.get(f"{base}/result", headers=headers)
            except httpx.HTTPError as exc:
                raise ExecutionEnvironmentUnavailable(
                    f"{binary} ran but its exit code could not be read from {base}: {exc}"
                ) from exc
    finally:
        if handle is not None:
            handle.close()

    if verdict.status_code != 200:
        raise ExecutionEnvironmentUnavailable(
            f"{binary} ran but the agent reported no verdict at {base}: HTTP {verdict.status_code}"
        )
    try:
        body = verdict.json()
        result = EEResult(
            bin=str(body.get("bin", binary)),
            rc=int(body["rc"]),
            error=str(body.get("error", "")),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ExecutionEnvironmentUnavailable(
            f"{binary} ran but the agent's verdict was unreadable: {exc}"
        ) from exc

    log.info(
        "execution environment finished",
        binary=result.bin,
        rc=result.rc,
        agent_error=result.error or None,
    )
    return result


def cancel() -> bool:
    """Ask the EE to stop what it is running; whether there was anything to stop.

    An endpoint rather than a signal, because the agent is ansible's PARENT and
    can signal it directly -- which is why the Job deliberately does not set
    `shareProcessNamespace` (#2154). Best-effort: cancellation races a child that
    may already have finished, and reporting failure to cancel a finished run is
    noise rather than information.
    """
    try:
        base, headers = _endpoint()
    except ExecutionEnvironmentUnavailable:
        return False
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            response = client.post(f"{base}/cancel", headers=headers)
        if response.status_code != 202:
            return False
        return bool(response.json().get("signalled", False))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("could not reach the execution environment to cancel", error=str(exc))
        return False
