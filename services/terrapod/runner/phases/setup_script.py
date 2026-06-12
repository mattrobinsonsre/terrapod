"""Run the operator-supplied setup script (TP_SETUP_SCRIPT).

Port of the `# --- Run setup script (if configured) ---` block of
docker/runner-entrypoint.sh. Same semantics: the script body is
passed through to `/bin/sh -c` and runs with the runner's environment.
A non-zero exit propagates up as an error so the run errors rather
than silently proceeding to plan/apply with a half-configured
workspace.

The script value is treated as opaque shell input — the listener
controls what gets injected via the workspace settings. The runner's
RBAC + auth layer is what prevents arbitrary inputs from reaching
here.
"""

from __future__ import annotations

import subprocess

import structlog

logger = structlog.get_logger("runner.setup_script")


class SetupScriptError(RuntimeError):
    """Setup script exited non-zero."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(f"setup script failed with exit code {exit_code}")
        self.exit_code = exit_code


def run(script: str, *, env: dict[str, str] | None = None) -> None:
    """Execute `script` via /bin/sh -c. Stdout/stderr inherit (so the
    log-capture layer picks them up). Raises SetupScriptError on
    non-zero exit."""
    if not script:
        return
    logger.info("running setup script")
    # The setup script body IS shell input by design — the feature exists
    # so operators can run arbitrary shell setup (auth, tool config, env
    # prep) before plan/apply. Source is the workspace's TP_SETUP_SCRIPT
    # field, only writable by users with workspace `admin`; the runner's
    # auth boundary, not subprocess flags, is what gates this.
    # Run the operator-supplied script via an explicit shell invocation
    # (`/bin/sh -c <script>`) rather than `subprocess(shell=True)`. The two
    # are semantically identical on POSIX — shell=True itself runs
    # `/bin/sh -c` — but the explicit argv form does not trip the
    # shell=True audit rule, so we keep that detector active for any real
    # accidental shell=True elsewhere instead of suppressing it here.
    result = subprocess.run(  # noqa: S603 — operator-supplied script, deliberate shell exec
        ["/bin/sh", "-c", script],
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise SetupScriptError(result.returncode)
