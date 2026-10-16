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
from pathlib import Path


def var_file_args(var_files: Iterable[str], *, base_dir: Path | None = None) -> list[str]:
    """`-var-file=PATH` for each entry.

    `base_dir` resolves each RELATIVE entry against that directory, and exists
    because a second consumer of the same list may run the engine somewhere
    else (#2132). The plan runs in the PROCESS working directory, and on a
    Terragrunt run that is not the directory the orchestrator tracks as `cwd`:
    tofu works in `.terragrunt-cache/<hash>/<hash>/` while the process stays
    chdir'd to the unit dir where `terragrunt.hcl` lives. So a relative
    `-var-file` handed to a command run in the cache dir names a different file
    -- usually none -- than the plan reads, which turns a working run into a
    discovery failure for a reason nothing in the configuration explains.

    Omitted, the operator's paths are passed **verbatim, byte for byte**, which
    is what plan and init want: they run in the process working directory, so
    the paths already mean what the operator wrote, and normalising them here
    would change a command that works today.
    """
    out: list[str] = []
    for vf in var_files:
        if not vf:
            continue
        if base_dir is None:
            out.append(f"-var-file={vf}")
            continue
        path = Path(vf)
        out.append(f"-var-file={path if path.is_absolute() else base_dir / path}")
    return out


def target_args(addrs: Iterable[str]) -> list[str]:
    """`-target=ADDR` for each entry. Used on plan AND apply."""
    return [f"-target={a}" for a in addrs if a]


def replace_args(addrs: Iterable[str]) -> list[str]:
    """`-replace=ADDR` for each entry. Plan phase only."""
    return [f"-replace={a}" for a in addrs if a]


def graph_supports_var_file(binary: str) -> bool:
    """Whether `<binary> graph` accepts `-var-file` (#2132).

    Its own probe rather than reusing `init_supports_var_file`, because the two
    commands gained the flag independently and the answer genuinely differs:
    measured against OpenTofu 1.12.6, `graph -help` advertises `-var` and
    `-var-file`, while the same binary's `init` support arrived separately
    (tofu >= 1.12 / terraform >= 1.10). Asking the wrong command is how a
    capability check comes to be confidently wrong.

    Probes the help output for the same reason the sibling does: it covers both
    engines, cannot be wrong about the specific binary just downloaded, and
    needs no version table to maintain.

    **`-input=false` is deliberately not probed, because `graph` rejects it.**
    Measured: `flag provided but not defined: -input`. The no-prompt guarantee
    comes from giving the subprocess no stdin instead, which no engine can
    refuse.
    """
    try:
        result = subprocess.run(  # noqa: S603 — binary is operator-supplied
            [binary, "graph", "-help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # OSError, not FileNotFoundError: a binary that exists but cannot be
        # executed raises `OSError: [Errno 8] Exec format error` (a wrong-arch
        # download from the binary cache), and a runner at its fork or memory
        # limit raises ENOMEM. Both are "cannot tell", which is what False
        # means here -- and the narrow form let them escape a function whose
        # only contract is to answer a question, into a caller documented as
        # never raising. `SubprocessError` keeps `TimeoutExpired`, which is not
        # an `OSError`.
        return False
    return "-var-file" in (result.stdout + result.stderr)


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
        # limit raises ENOMEM. Both are "cannot tell", which is what False
        # means here -- and the narrow form let them escape a function whose
        # only contract is to answer a question, into a caller documented as
        # never raising. `SubprocessError` keeps `TimeoutExpired`, which is not
        # an `OSError`.
        return False
    return "-var-file" in (result.stdout + result.stderr)
