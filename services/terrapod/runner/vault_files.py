"""Where a Vault-sourced variable delivered as a file lands in the runner (#1619).

Shared by the API (which validates names on write and again when a run is
claimed, and substitutes each variable's value with the file's path) and the
listener (which re-validates before it builds any mount). Deliberately pure
stdlib so the minimal listener image can import it.

Two kinds of name:

- a relative path ``a/b.json`` lands at ``/var/run/terrapod/files/a/b.json``,
  inside one read-only Secret volume;
- a home path ``~/x/y`` lands at ``$HOME/x/y`` (``/home/runner/x/y``) through a
  ``subPath`` mount of the same per-run Secret.

A name never carries a value, and nothing in this module ever sees one.
"""

from __future__ import annotations

import posixpath
import re

FILES_DIR = "/var/run/terrapod/files"
#: The runner's HOME. job_template sets HOME to this on every Job and mounts an
#: emptyDir here; Dockerfile.runner creates it for UID 1000.
RUNNER_HOME = "/home/runner"
HOME_PREFIX = "~/"

MAX_NAME_LEN = 255
MAX_FILE_BYTES = 256 * 1024

_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")

#: Paths under HOME the runner itself writes or that the tools it drives
#: consult, relative to HOME. A file mounted at (or above) any of them would
#: shadow or block what the runner configures, so they are refused.
#:
#: - ``.config/terrapod-git``: the git_auth phase's gitconfig, credential store
#:   and ssh config (runner/phases/git_auth.py).
#: - ``.gitconfig``, ``.ssh``: git's and ssh's own per-user config — the
#:   git_auth phase owns module-fetch credentials, and a mounted file here would
#:   silently change how every module source is fetched.
#: - ``.terraformrc``, ``.terraform.rc``, ``.terraform.d``: the terraform/tofu
#:   CLI config, credentials and plugin cache. The runner supplies CLI config
#:   (provider mirror, registry credentials) via TF_CLI_CONFIG_FILE.
#: - ``.pulumi``: the Pulumi CLI's home (PULUMI_HOME defaults to it): the
#:   plugins it installs through Terrapod's proxy and its workspace state. A
#:   read-only file mounted there would block a Pulumi run's plugin install.
MANAGED_HOME_PATHS: tuple[str, ...] = (
    ".config/terrapod-git",
    ".gitconfig",
    ".ssh",
    ".terraformrc",
    ".terraform.rc",
    ".terraform.d",
    ".pulumi",
)


class FilePathError(ValueError):
    """A file name is not deliverable. The message is safe to show: names only."""


def is_home(name: str) -> bool:
    return name.startswith(HOME_PREFIX)


def validate_name(name: object) -> str:
    """Return ``name`` if it is a deliverable file name, else raise FilePathError.

    The error message is the *reason* only; callers prefix it with the variable.
    """
    if not isinstance(name, str):
        raise FilePathError("must be a string")
    if name == "":
        raise FilePathError("must not be empty")
    if "\x00" in name:
        raise FilePathError("contains a NUL character")
    if len(name) > MAX_NAME_LEN:
        raise FilePathError(f"is longer than {MAX_NAME_LEN} characters")
    home = is_home(name)
    rel = name[len(HOME_PREFIX) :] if home else name
    if rel.startswith("/"):
        raise FilePathError(
            "must be a relative path, or start with ~/ for a path in the runner's "
            "home directory; absolute paths are not allowed"
        )
    for seg in rel.split("/"):
        if seg == "":
            raise FilePathError("has an empty path segment")
        if seg in (".", ".."):
            raise FilePathError("has a '.' or '..' path segment")
        if not _SEGMENT.fullmatch(seg):
            raise FilePathError(
                f"has a path segment with characters outside [A-Za-z0-9._-]: {seg!r}"
            )
    if home:
        for managed in MANAGED_HOME_PATHS:
            if rel == managed or rel.startswith(managed + "/") or managed.startswith(rel + "/"):
                raise FilePathError(
                    f"targets ~/{managed}, which the runner manages itself; choose another path"
                )
    return name


def target_path(name: str) -> str:
    """The absolute path a (validated) name is materialized at."""
    if is_home(name):
        return posixpath.join(RUNNER_HOME, name[len(HOME_PREFIX) :])
    return posixpath.join(FILES_DIR, name)


def home_parent_dirs(names: list[str]) -> list[str]:
    """Directories under HOME that must exist before the home files are mounted.

    Sorted, deduplicated, and excluding HOME itself. The runner creates them as
    its own UID so the directory beside a mounted file stays writable (the
    container runtime would otherwise create it root-owned).
    """
    dirs: set[str] = set()
    for n in names:
        if not is_home(n):
            continue
        parent = posixpath.dirname(target_path(n))
        while parent != RUNNER_HOME and parent.startswith(RUNNER_HOME + "/"):
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    return sorted(dirs)


def check_collisions(entries: list[tuple[str, str]]) -> None:
    """Refuse two files at one path, or a file where another needs a directory.

    ``entries`` is ``[(variable_key, name)]`` after variable-set precedence.
    Raises FilePathError naming both variables.
    """
    targets = sorted((target_path(name), key) for key, name in entries)
    for i, (t1, k1) in enumerate(targets):
        for t2, k2 in targets[i + 1 :]:
            if t1 == t2:
                raise FilePathError(
                    f"variables {k1!r} and {k2!r} both deliver a Vault file to {t1}"
                )
            if t2.startswith(t1 + "/"):
                raise FilePathError(
                    f"variable {k1!r} delivers a Vault file to {t1}, which variable "
                    f"{k2!r} needs as a directory for {t2}"
                )


def secret_key(index: int) -> str:
    """The per-run vars Secret key that holds the index-th file's content.

    Secret keys cannot contain ``/``, so a key is derived rather than the name
    itself; the Job maps each key back to its path.
    """
    return f"vault-file-{index}"
