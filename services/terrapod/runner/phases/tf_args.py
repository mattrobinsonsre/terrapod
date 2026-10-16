"""Build terraform/tofu CLI args from the JSON env vars the listener
injects.

Port of:
  - `# --- Build -var-file args BEFORE init ---`
  - `# --- Build -target arguments from TP_TARGET_ADDRS JSON ---`
  - `# --- Build -replace arguments from TP_REPLACE_ADDRS JSON ---`

Each function returns a list of argv pieces ready to append to a
subprocess command line.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable


def var_file_args(var_files: Iterable[str]) -> list[str]:
    """`-var-file=PATH` for each entry."""
    return [f"-var-file={vf}" for vf in var_files if vf]


def target_args(addrs: Iterable[str]) -> list[str]:
    """`-target=ADDR` for each entry. Used on plan AND apply."""
    return [f"-target={a}" for a in addrs if a]


def replace_args(addrs: Iterable[str]) -> list[str]:
    """`-replace=ADDR` for each entry. Plan phase only."""
    return [f"-replace={a}" for a in addrs if a]


def init_supports_var_file(binary: str) -> bool:
    """Detect whether `<binary> init` accepts `-var-file`. tofu >= 1.12
    and terraform >= 1.10 support it for early-evaluation configs.

    We probe the binary's `init -help` output rather than parsing a
    version string — covers both terraform and tofu, future-proof
    against new versions adding the flag, never wrong about the
    specific binary we just downloaded.
    """
    try:
        result = subprocess.run(  # noqa: S603 — binary is operator-supplied
            [binary, "init", "-help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # OSError, not FileNotFoundError: a binary that exists but cannot be
        # executed raises `OSError: [Errno 8] Exec format error` (a wrong-arch
        # download from the binary cache), and a runner at its fork or memory
        # limit raises ENOMEM. Both mean "cannot tell", which is what False
        # says. The narrow form let them escape instead, and `run_init` calls
        # this OUTSIDE its own try, so the runner died with a traceback where
        # it should have raised a clean InitError. `SubprocessError` keeps
        # `TimeoutExpired`, which is not an `OSError`.
        return False
    return "-var-file" in (result.stdout + result.stderr)
