"""Module discovery (#1584): the modules in a repository, proposed for the registry.

A scan walks one repository's file tree and proposes every directory that
holds Terraform/OpenTofu files — the repository root and any submodules
(#1583) — each with a suggested registry name and provider. Nothing is stored:
the operator picks which proposals to register, and each goes through the
ordinary module-create path with its ``subdirectory``.

Deciding what counts as a module needs no file contents. Any directory with
``.tf`` files is a candidate; directories that conventionally hold something
else (examples, tests, fixtures, tooling) are left out; and the operator makes
the final call, so a false positive costs an unticked box rather than a
registration.
"""

import posixpath
import re

# A module's configuration files. Variable files (.tfvars) belong to a root
# configuration, not a module, so they do not make a directory a candidate.
_TF_SUFFIXES = (".tf", ".tf.json")

# Directories that conventionally hold something other than a module to publish.
# Hidden directories (.github, .terraform, ...) are skipped separately.
_SKIP_SEGMENTS = frozenset({"examples", "example", "test", "tests", "testdata", "fixtures"})

# The module-create form's rule: a lowercase letter, then lowercase letters,
# digits and hyphens.
_NOT_NAME_CHARS = re.compile(r"[^a-z0-9]+")
_MAX_NAME_LENGTH = 64

# The registry's own naming convention for a module repository.
_CONVENTIONAL_REPO = re.compile(r"^terraform-([a-z0-9]+)-(.+)$")


def candidate_directories(file_paths: list[str]) -> list[str]:
    """Every directory holding Terraform files, the repository root (``""``)
    first, then by path."""
    dirs: set[str] = set()
    for path in file_paths:
        if not path.endswith(_TF_SUFFIXES):
            continue
        directory = posixpath.dirname(path)
        segments = directory.split("/") if directory else []
        if any(s in _SKIP_SEGMENTS or s.startswith(".") for s in segments):
            continue
        dirs.add(directory)
    return sorted(dirs, key=lambda d: (d != "", d))


def repo_name_from_url(repo_url: str) -> str:
    """The repository's own name: the last path segment, without ``.git``."""
    return repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def suggest_provider(repo_name: str) -> str:
    """The provider from a ``terraform-<provider>-<name>`` repository, else ``""``."""
    m = _CONVENTIONAL_REPO.match(repo_name.lower())
    return m.group(1) if m else ""


def module_base_name(repo_name: str) -> str:
    """The repository's module name: its ``terraform-<provider>-`` prefix
    dropped, when it follows that convention."""
    base = repo_name.lower()
    m = _CONVENTIONAL_REPO.match(base)
    return m.group(2) if m else base


def fit_name(candidate: str) -> str:
    """``candidate`` fitted to the create form's rule: lowercase letters, digits
    and hyphens, starting with a letter, at most 64 characters."""
    name = _NOT_NAME_CHARS.sub("-", candidate.lower()).strip("-")
    if not name:
        return "module"
    if not name[0].isalpha():
        name = f"m-{name}"
    return name[:_MAX_NAME_LENGTH].rstrip("-")


def suggest_name(repo_name: str, subdirectory: str) -> str:
    """A registry name for the module at ``subdirectory``.

    The repository's module name, followed for a submodule by the last segment
    of its path: ``terraform-azurerm-management-groups`` + ``modules/create``
    gives ``management-groups-create``. Fitted to the create form's rule.
    """
    parts = [module_base_name(repo_name)]
    if subdirectory:
        parts.append(subdirectory.rsplit("/", 1)[-1])
    return fit_name("-".join(parts))
