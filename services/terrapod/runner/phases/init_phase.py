"""terraform/tofu init invocation.

Port of the `# --- Initialize ---` block of docker/runner-entrypoint.sh.

Init runs with the var-file args built earlier (when the binary
supports it — tofu >= 1.12 / terraform >= 1.10 for early-evaluation
configs) but never with -target / -replace (init doesn't accept them).
We pass the resulting subprocess through the existing
`exec_subprocess.run` for log capture; signal forwarding is set up by
the orchestrator's main()-level signal handler.
"""

from __future__ import annotations

import structlog

from terrapod.runner import exec_subprocess
from terrapod.runner.phases.tf_args import init_supports_var_file

logger = structlog.get_logger("runner.init_phase")


class InitError(RuntimeError):
    """init exited non-zero. Caller must propagate the exit code."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(f"init failed with exit code {exit_code}")
        self.exit_code = exit_code


def run_init(
    *,
    binary: str,
    var_file_args: list[str],
    log_file: str,
    child_grace_seconds: float = 25.0,
) -> None:
    """Run `<binary> init -input=false [var_file_args...]` via
    exec_subprocess, redirecting combined stdout/stderr to log_file.

    Raises InitError on non-zero exit so the orchestrator can short-
    circuit (skip plan/apply, still flush the log)."""
    argv = [binary, "init", "-input=false"]
    if var_file_args:
        if init_supports_var_file(binary):
            argv.extend(var_file_args)
        else:
            logger.warning(
                "init does not accept -var-file (need tofu>=1.12 / terraform>=1.10 "
                "for early-eval); init will run without var-files. Configs using "
                "variables in backend/required_providers/module-source will fail.",
                binary=binary,
            )

    logger.info("running init", binary=binary, argv_tail=argv[1:])
    result = exec_subprocess.run(
        argv,
        log_file=log_file,
        child_grace_seconds=child_grace_seconds,
        tee_to_stdout=True,
    )
    if result.exit_code != 0:
        raise InitError(result.exit_code)
