"""Resolve and chdir into the monorepo subdirectory for plan/apply.

Port of the `# --- Change to working directory ---` block of
docker/runner-entrypoint.sh. Same path-traversal guard.
"""

from __future__ import annotations

import os
from pathlib import Path


class WorkingDirectoryError(RuntimeError):
    """Bad TP_WORKING_DIR — path traversal or missing target."""


def resolve_and_chdir(work_dir: Path, working_dir: str) -> Path:
    """If `working_dir` is non-empty, sanitise and chdir under
    `work_dir`. Returns the new cwd. No-op (returns `work_dir`) if
    `working_dir` is empty.

    Sanitisation matches bash:
      - strip leading and trailing slashes
      - reject any segment containing `..`
      - the target directory must exist after extracting the config
    """
    if not working_dir:
        os.chdir(work_dir)
        return work_dir

    sanitised = working_dir.strip("/")
    if ".." in sanitised.split("/"):
        raise WorkingDirectoryError(f"working directory contains path traversal: {working_dir!r}")

    target = (work_dir / sanitised).resolve()
    # Belt-and-braces: refuse anything that escapes work_dir even via
    # symlink trickery.
    if not str(target).startswith(str(work_dir.resolve())):
        raise WorkingDirectoryError(f"working directory resolves outside work_dir: {working_dir!r}")
    if not target.is_dir():
        raise WorkingDirectoryError(f"working directory '{working_dir}' not found in config")

    os.chdir(target)
    return target
