"""Backend backstop — verify init configured the local backend.

Port of the `# --- Backend backstop (#346) ---` block of
docker/runner-entrypoint.sh.

The runner MUST execute on the local backend; a remote backend inside
the Job recurses straight back into Terrapod. The
zzzz_terrapod_backend_override.tf file (written during the
configuration phase) forces local via terraform's override-file merge
— but a user file sorting even later than `zzzz_…` could displace it.
After init, read what backend terraform actually configured (recorded
in `.terraform/terraform.tfstate`) and fail loudly if it isn't local.
"""

from __future__ import annotations

import json
from pathlib import Path


class BackendBackstopError(RuntimeError):
    """init configured something other than the local backend.
    Caller should exit non-zero with a clear message."""


def verify_local_backend(strip_dir: Path) -> str:
    """Read `.terraform/terraform.tfstate` under `strip_dir` and return
    the configured backend type. Raises `BackendBackstopError` if
    anything other than `local` is configured (including a missing or
    unreadable state file)."""
    state_path = strip_dir / ".terraform" / "terraform.tfstate"
    backend_type = "MISSING"
    try:
        with state_path.open() as f:
            data = json.load(f)
        backend_type = data.get("backend", {}).get("type") or "MISSING"
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        backend_type = "MISSING"

    if backend_type != "local":
        raise BackendBackstopError(
            f"expected the local backend after init, got {backend_type!r}. "
            "A user-supplied override file appears to have displaced the "
            "Terrapod backend override. Remove any committed override.tf / "
            "*_override.tf that declares a 'backend' or 'cloud' block."
        )

    return backend_type
