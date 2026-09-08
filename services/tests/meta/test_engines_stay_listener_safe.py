"""The engine package must stay importable inside the listener image (#1487).

The listener image carries only `config`, `logging_config`, `http_retry`,
`engines/` and `runner/` — it has no DB layer and no SQLAlchemy. The listener
builds its Job spec through the engine strategy, so `engines/` must import
nothing at module scope that the image does not ship.

This is a real hazard for the phases that follow rather than a theoretical one.
#1489 moves *terminal run resolution* behind this same strategy, and the obvious
way to write that is to take a `Run` — which would pull `terrapod.db.models`, and
SQLAlchemy behind it, into an image that has neither. The failure would not
appear in any unit test; it would appear as the listener crash-looping on import
in a real deployment.

The fix when this fails is not to widen the allow-list. It is to take plain
values, or to import the model under `TYPE_CHECKING`.
"""

from __future__ import annotations

import ast
import pathlib

ENGINES = pathlib.Path(__file__).resolve().parents[2] / "terrapod/engines"


def _find_upwards(relative: str) -> pathlib.Path | None:
    """Locate a repo file from either layout.

    Locally the repo root is `parents[3]`; inside the test image the tree is
    flattened to `/app`, making it `parents[2]`. Searching upward works in both
    rather than hard-coding one and silently skipping in the other — a test that
    only runs on a laptop is not a guard.
    """
    here = pathlib.Path(__file__).resolve()
    for base in here.parents[1:6]:
        candidate = base / relative
        if candidate.exists():
            return candidate
    return None


DOCKERFILE = _find_upwards("docker/Dockerfile.listener")

#: What the listener image actually contains. Anything else imported at module
#: scope by `engines/` is absent at runtime there.
LISTENER_MODULES = {
    "terrapod.config",
    "terrapod.logging_config",
    "terrapod.http_retry",
    "terrapod.engines",
    "terrapod.runner",
}


def _module_scope_imports(tree: ast.Module) -> set[str]:
    """Imports evaluated at import time, ignoring those inside functions.

    A lazy import inside a method is fine — it only runs if that path is taken,
    and the strategy's `build_job_spec` deliberately defers `runner.job_template`
    so the API never pays for it.
    """
    names: set[str] = set()
    for node in tree.body:  # module scope only, not ast.walk
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif isinstance(node, ast.If):
            # `if TYPE_CHECKING:` blocks never execute at runtime.
            test = ast.dump(node.test)
            if "TYPE_CHECKING" in test:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    names.update(a.name for a in sub.names)
                elif isinstance(sub, ast.ImportFrom) and sub.module and sub.level == 0:
                    names.add(sub.module)
    return names


def test_engines_imports_nothing_the_listener_image_lacks():
    offenders: list[str] = []
    for path in sorted(ENGINES.glob("*.py")):
        for mod in _module_scope_imports(ast.parse(path.read_text())):
            if not mod.startswith("terrapod"):
                continue
            root = ".".join(mod.split(".")[:2])
            if root not in LISTENER_MODULES:
                offenders.append(f"{path.name}: imports {mod} at module scope")

    assert not offenders, (
        "engines/ must import only what the listener image ships "
        f"({', '.join(sorted(LISTENER_MODULES))}):\n  " + "\n  ".join(offenders) + "\n"
        "Take plain values or import under TYPE_CHECKING — do not widen the image."
    )


def test_the_listener_image_actually_copies_engines():
    """The invariant above is worthless if the package is not shipped at all.

    A module the listener imports but the Dockerfile does not COPY fails at
    startup, in a deployment, with an ImportError — and nothing in the test suite
    would notice, because tests run against the full source tree.
    """
    assert DOCKERFILE is not None, (
        "docker/Dockerfile.listener was not found from either layout — if the "
        "test image stopped copying docker/, this guard went quiet rather than red"
    )
    dockerfile = DOCKERFILE.read_text()
    assert "COPY services/terrapod/engines/" in dockerfile, (
        "Dockerfile.listener does not copy terrapod/engines/, but the listener "
        "imports it — the image would crash on startup"
    )
