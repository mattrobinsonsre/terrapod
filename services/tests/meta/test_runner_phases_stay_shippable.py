"""Everything the runner image ships must import only what it also ships (#1523).

The runner image copies a *hand-listed* set of modules — not the package — so
`terrapod.config`, `terrapod.logging_config`, `terrapod.db` and everything else
simply do not exist at runtime there. Tests run against the whole source tree, so
an import of one costs nothing in CI and crashes the container the moment the
orchestrator reaches that phase.

That is not hypothetical. `phases/pulumi_exec.py` imported
`terrapod.logging_config.get_logger` — a one-line wrapper around
`structlog.get_logger`, which every other phase calls directly — and every Pulumi
run died on `ModuleNotFoundError: No module named 'terrapod.logging_config'`
after the Job had started, downloaded its configuration and its state. Nothing
before a live run could have said so.

The allow-list is derived from `Dockerfile.runner` rather than written down here,
so adding a COPY is the single act that makes a module importable, and this guard
cannot drift away from the image it is describing.

When this fails, the fix is usually to use the dependency the image already has
(structlog, httpx) or to move the import inside the function that needs it *and*
add the COPY. Widening the image is the last resort, not the first.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


def _find_upwards(relative: str) -> pathlib.Path | None:
    """Locate a repo file from either layout.

    Locally the repo root is `parents[3]`; inside the test image the tree is
    flattened, making it `parents[2]`. Searching upward works in both rather than
    hard-coding one and silently skipping in the other.
    """
    here = pathlib.Path(__file__).resolve()
    for base in here.parents[1:6]:
        candidate = base / relative
        if candidate.exists():
            return candidate
    return None


DOCKERFILE = _find_upwards("docker/Dockerfile.runner")
SERVICES = pathlib.Path(__file__).resolve().parents[2]


def _copied() -> tuple[set[str], set[str], list[pathlib.Path]]:
    """Read the image's COPY list: (exact modules, package prefixes, source files).

    Keyed on the COPY *destination*, because two of them do not mirror their
    source — `services/upstream_keys/` lands at `runner/upstream_keys/`. Deriving
    from the source path would describe a package layout the image does not have.
    """
    assert DOCKERFILE is not None, (
        "docker/Dockerfile.runner was not found from either layout — if the test "
        "image stopped copying docker/, this guard went quiet rather than red"
    )

    exact: set[str] = set()
    prefixes: set[str] = set()
    sources: list[pathlib.Path] = []

    for line in DOCKERFILE.read_text().splitlines():
        if not line.startswith("COPY services/terrapod"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        src, dest = parts[1], parts[2]
        dest = dest.removeprefix("./")
        if not dest.startswith("terrapod/"):
            # e.g. saas_known_hosts, which lands in /etc/ssh — data, not a module.
            continue

        src_path = SERVICES / src.removeprefix("services/")
        if dest.endswith("/"):
            prefixes.add(dest.rstrip("/").replace("/", "."))
            if src_path.is_dir():
                sources.extend(sorted(src_path.rglob("*.py")))
        elif dest.endswith(".py"):
            module = dest[: -len(".py")].replace("/", ".")
            # `runner/__init__.py` ships the package itself, not a module named
            # `__init__` — so record the package.
            exact.add(module.removesuffix(".__init__"))
            if src_path.is_file():
                sources.append(src_path)

    return exact, prefixes, sources


def _is_shipped(module: str, exact: set[str], prefixes: set[str]) -> bool:
    if module in exact:
        return True
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _imports(tree: ast.Module) -> set[str]:
    """Every terrapod import that can execute, at any nesting depth.

    Unlike the listener guard, a lazy import is *not* excused here: the runner
    takes these paths by design (the orchestrator imports its phase modules
    inside the function that dispatches to them), so an import deferred into a
    function still crashes the run when that phase is reached. Only
    `TYPE_CHECKING` blocks are exempt, because they never execute.
    """
    names: set[str] = set()

    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test):
            skip.update(id(sub) for sub in ast.walk(node))

    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            # `from terrapod.runner import exec_subprocess` needs
            # `terrapod.runner.exec_subprocess` shipped, not just the package.
            # Only treat the name as a submodule when a matching source file
            # exists, so imported *symbols* are not mistaken for modules.
            pkg = SERVICES / node.module.replace(".", "/")
            for alias in node.names:
                if (pkg / f"{alias.name}.py").is_file() or (pkg / alias.name).is_dir():
                    names.add(f"{node.module}.{alias.name}")
    return {n for n in names if n.startswith("terrapod")}


def test_the_runner_image_ships_everything_its_modules_import():
    exact, prefixes, sources = _copied()
    assert sources, "no runner sources resolved from the COPY list — guard is inert"

    offenders: list[str] = []
    for path in sources:
        for module in sorted(_imports(ast.parse(path.read_text()))):
            if not _is_shipped(module, exact, prefixes):
                offenders.append(f"{path.relative_to(SERVICES)}: imports {module}")

    assert not offenders, (
        "these modules are imported by the runner image but never COPYed into it, "
        "so they raise ModuleNotFoundError at runtime while every test passes:\n  "
        + "\n  ".join(offenders)
        + "\n\nShipped: "
        + ", ".join(sorted(exact | {p + ".*" for p in prefixes}))
        + "\nPrefer a dependency the image already has (structlog, httpx) over "
        "widening the image."
    )


@pytest.mark.parametrize("absent", ["terrapod.logging_config", "terrapod.config"])
def test_the_guard_would_catch_the_regression(absent: str):
    """The guard is only worth having if it rejects the thing that broke.

    Pinned against both the module that actually caused it and `terrapod.config`,
    the other tempting one — neither is in the image, and a future COPY that adds
    one should be a deliberate act that turns this test red.
    """
    exact, prefixes, _ = _copied()
    assert not _is_shipped(absent, exact, prefixes), (
        f"{absent} is now shipped in the runner image; if that was intentional, "
        "update this test — but check the image size and dependency weight first"
    )
