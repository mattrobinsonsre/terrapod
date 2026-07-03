"""Run operator-defined execution hooks at fixed points in the run (#619).

A hook is a reusable custom-shell step, resolved server-side and delivered into
the runner Job via the per-run vars Secret as a mounted JSON file. The
orchestrator calls ``run_point(point, ...)`` at each of the five boundaries
(pre_init/pre_plan/post_plan/pre_apply/post_apply); this module reads the file,
selects the hooks for that point (in the order the server already sorted them —
priority then name), and runs each via ``/bin/sh -c``.

Semantics mirror ``setup_script``: the body is opaque shell input run with the
runner's environment (and cloud identity). A non-zero exit raises ``HookError``,
which the orchestrator turns into a non-zero run exit so the failure is
surfaced rather than silently passed. The script bodies are non-sensitive by
contract; any secret a hook needs arrives via the same per-run vars Secret.

The kill-switch (``runners.hooks.enabled``) is enforced upstream at the listener
— when disabled, no hooks file is written, so ``run_point`` finds nothing and
no-ops.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger("runner.execution_hooks")

# Mounted from the per-run vars Secret. Keep in sync with runner/job_template.py
# (_HOOKS_SECRET_KEY / _HOOKS_FILENAME) and the listener's _create_vars_secret.
_HOOKS_FILE = Path("/var/run/terrapod/vars/execution-hooks.json")


class HookError(RuntimeError):
    """An execution hook exited non-zero."""

    def __init__(self, hook_point: str, name: str, exit_code: int) -> None:
        super().__init__(
            f"execution hook '{name}' ({hook_point}) failed with exit code {exit_code}"
        )
        self.hook_point = hook_point
        self.name = name
        self.exit_code = exit_code


def _load() -> list[dict]:
    """Read the mounted hooks file. Returns [] when absent/unreadable — hooks
    are optional and absence must never fail the run."""
    if not _HOOKS_FILE.exists():
        return []
    try:
        data = json.loads(_HOOKS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("failed to read execution hooks file", error=str(exc))
        return []
    return data if isinstance(data, list) else []


def run_point(hook_point: str, *, env: dict[str, str] | None = None) -> None:
    """Run every hook associated with ``hook_point``, in delivered order.

    Raises HookError on the first hook that exits non-zero (stops the rest —
    the run is going to fail anyway)."""
    for hook in _load():
        if hook.get("hook_point") != hook_point:
            continue
        script = hook.get("script") or ""
        name = hook.get("name") or "(unnamed)"
        if not script.strip():
            continue
        logger.info("running execution hook", hook_point=hook_point, name=name)
        # Operator-supplied shell body by design (see module docstring). Explicit
        # `/bin/sh -c` argv rather than shell=True keeps the shell=True audit rule
        # active elsewhere — identical semantics on POSIX.
        result = subprocess.run(  # noqa: S603 — operator-supplied script, deliberate shell exec
            ["/bin/sh", "-c", script],
            check=False,
            env=env,
        )
        if result.returncode != 0:
            raise HookError(hook_point, name, result.returncode)
