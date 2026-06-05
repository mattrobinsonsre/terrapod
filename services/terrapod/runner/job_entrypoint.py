"""Entrypoint for runner K8s Jobs.

Owns the whole life of a single Terrapod run inside a Job pod: parses
the listener-supplied env vars, drives each phase, then hands off to
the bash script for phases not yet ported.

Successor to docker/runner-entrypoint.sh — phases migrate from bash
to Python one PR at a time. The bash script honors per-phase
TP_RUNNER_*_DONE env-var markers set here so an already-handled phase
is a no-op when bash inherits control.

Invocation: `python -m terrapod.runner.job_entrypoint` from the Job
spec. Env vars set by the listener are inherited verbatim.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog

from terrapod.runner.phases.binary import BinaryDownloadError, download_binary
from terrapod.runner.phases.configuration import download_configuration
from terrapod.runner.phases.state import download_state, reuse_plan_lock_file
from terrapod.runner.runner_config import RunnerConfig

# Where docker/runner-entrypoint.sh is copied to in the runner image.
_BASH_ENTRYPOINT_PATH = "/entrypoint.sh"

# The K8s Job emptyDir mount we treat as the run's working directory.
# Matches the Dockerfile's `WORKDIR /workspace`.
_DEFAULT_WORK_DIR = Path("/workspace")


def _configure_logging() -> None:
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


def _exec_bash(env: dict[str, str], argv: list[str]) -> int:
    log = structlog.get_logger("runner.job_entrypoint")

    if not os.path.exists(_BASH_ENTRYPOINT_PATH):
        log.error("Bash entrypoint not found", path=_BASH_ENTRYPOINT_PATH)
        return 127
    if not os.access(_BASH_ENTRYPOINT_PATH, os.X_OK):
        log.error("Bash entrypoint not executable", path=_BASH_ENTRYPOINT_PATH)
        return 126

    log.info("Delegating remaining phases to bash", path=_BASH_ENTRYPOINT_PATH)
    os.execvpe(_BASH_ENTRYPOINT_PATH, [_BASH_ENTRYPOINT_PATH, *argv], env)
    # execvpe only returns on error; raise to avoid silent fall-through.
    return 1


def _run_phases(cfg: RunnerConfig, work_dir: Path) -> dict[str, str]:
    """Drive the ported phases. Returns env-var deltas the bash sub-
    phase needs to inherit (TP_BIN, WORK_DIR, and TP_RUNNER_*_DONE
    markers).
    """
    log = structlog.get_logger("runner.job_entrypoint")
    env_delta: dict[str, str] = {}

    # Phase: binary cache download. Hard-fails the run on cache + upstream
    # both unreachable; the listener will mark the run errored.
    binary_path = download_binary(cfg)
    env_delta["TP_BIN"] = str(binary_path)
    env_delta["TP_RUNNER_BINARY_DONE"] = "1"

    # Phase: configuration tarball.
    work_dir.mkdir(parents=True, exist_ok=True)
    config_result = download_configuration(cfg, work_dir=work_dir)
    env_delta["TP_RUNNER_CONFIGURATION_DONE"] = "1"
    if config_result.downloaded and config_result.override_file is not None:
        env_delta["TP_RUNNER_OVERRIDE_FILE"] = str(config_result.override_file)
    # `strip_dir` may differ from work_dir for monorepos. Bash needs to chdir
    # to it before init.
    env_delta["TP_RUNNER_STRIP_DIR"] = str(config_result.strip_dir)

    # Phase: current state.
    state_present = download_state(cfg, strip_dir=config_result.strip_dir)
    env_delta["TP_RUNNER_STATE_DONE"] = "1"
    if state_present:
        env_delta["TP_RUNNER_STATE_PRESENT"] = "1"

    # Phase: apply-only lock-file reuse.
    if cfg.phase == "apply":
        lock_reused = reuse_plan_lock_file(cfg, strip_dir=config_result.strip_dir)
        env_delta["TP_RUNNER_LOCK_FILE_DONE"] = "1"
        if lock_reused:
            env_delta["TP_RUNNER_LOCK_FILE_REUSED"] = "1"

    log.info("Python-ported phases complete", env_delta=env_delta)
    return env_delta


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    log = structlog.get_logger("runner.job_entrypoint")

    if argv is None:
        argv = sys.argv[1:]

    work_dir_env = os.environ.get("WORK_DIR")
    work_dir = Path(work_dir_env) if work_dir_env else _DEFAULT_WORK_DIR

    try:
        cfg = RunnerConfig.from_env()
        env_delta = _run_phases(cfg, work_dir)
    except BinaryDownloadError as exc:
        log.error("Binary download failed", err=str(exc))
        return 1

    merged_env = {**os.environ, **env_delta}
    # WORK_DIR is read by bash even on dev invocations.
    merged_env.setdefault("WORK_DIR", str(work_dir))
    return _exec_bash(merged_env, argv)


if __name__ == "__main__":
    sys.exit(main())
