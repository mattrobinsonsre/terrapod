"""Entrypoint for runner K8s Jobs.

Owns the whole life of a single Terrapod run inside a Job pod: setup
config, signal handling, then drive the per-phase logic. Successor to
docker/runner-entrypoint.sh — the bash script still does the actual
work in this commit; this module exec's into it so the image-base +
invocation switch can land first without behavioural changes. Phases
move from bash to Python in subsequent commits.

Invocation: `python -m terrapod.runner.job_entrypoint` from the Job
spec. Env vars set by the listener are inherited verbatim.
"""

from __future__ import annotations

import os
import sys

import structlog

# Where docker/runner-entrypoint.sh is copied to in the runner image.
# Kept as a constant so the porting PRs can shadow individual phases
# in Python while still falling through to bash for the rest.
_BASH_ENTRYPOINT_PATH = "/entrypoint.sh"


def _configure_logging() -> None:
    """Structured logging that matches the API + listener output shape.

    The bash entrypoint already prefixes its lines with `[entrypoint]`;
    we keep that prefix for grep-compatibility with operators reading
    Loki for runner logs.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        cache_logger_on_first_use=True,
    )


def main(argv: list[str] | None = None) -> int:
    """Drive the run lifecycle for a single Job pod.

    Today: delegate to bash. Returns nothing because `os.execvp` only
    returns on error — the bash process replaces this one and owns the
    exit code from then on.

    Tomorrow: each phase from runner-entrypoint.sh lands here as its
    own function and the exec call goes away.
    """
    _configure_logging()
    log = structlog.get_logger("runner.job_entrypoint")

    if argv is None:
        argv = sys.argv[1:]

    if not os.path.exists(_BASH_ENTRYPOINT_PATH):
        log.error(
            "Bash entrypoint not found — runner image is misconfigured",
            path=_BASH_ENTRYPOINT_PATH,
        )
        return 127

    if not os.access(_BASH_ENTRYPOINT_PATH, os.X_OK):
        log.error(
            "Bash entrypoint not executable — runner image is misconfigured",
            path=_BASH_ENTRYPOINT_PATH,
        )
        return 126

    log.info(
        "Delegating to bash entrypoint",
        path=_BASH_ENTRYPOINT_PATH,
        argv=argv,
    )

    # execvp replaces the current process — signals to the bash child
    # are inherited as if the listener launched it directly. No PID-1
    # signal-forwarding wrapper needed at this stage.
    os.execvp(_BASH_ENTRYPOINT_PATH, [_BASH_ENTRYPOINT_PATH, *argv])

    # Unreachable; execvp only returns on error and raises OSError in
    # that case. Belt-and-braces in case of future refactor mistakes.
    return 1


if __name__ == "__main__":
    sys.exit(main())
