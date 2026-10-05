"""The runner image must contain every module the Job actually imports.

`docker/Dockerfile.runner` COPIES runner modules **one file at a time** rather
than the whole package, so adding a module under `services/terrapod/runner/` and
importing it from the entrypoint chain produces an image that is missing it. The
failure is total and arrives late: every runner Job dies at import with
`ModuleNotFoundError`, so no run of any kind can execute — and nothing in the
Python suite notices, because the module is present in the source tree that the
tests import from.

AGENTS.md records this as a known trap ("a new runner module needs its path added
to the Dockerfile.runner COPY list and to the Tiltfile's build-runner-image
deps"). It happened anyway, to `reserved_env.py`, and only the eval-boot job —
which builds the real image and runs a real Job — caught it. This is the cheap
version of that check.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


def _root() -> pathlib.Path:
    """The directory holding `docker/Dockerfile.runner`.

    Two layouts, and a fixed `parents[n]` is wrong in one of them. In a checkout
    this file is `<repo>/services/tests/runner/...` and the Dockerfile is at
    `<repo>/docker/`. In the test image `services/tests` is copied to `/app/tests`
    and `docker` to `/app/docker`, so the same climb lands on `/`. Searching
    upward for the file is correct in both — and the first version of this test,
    which hardcoded the climb, failed in CI with
    `FileNotFoundError: '/docker/Dockerfile.runner'`. A test about things missing
    from an image, undone by something missing from an image.
    """
    here = pathlib.Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "docker" / "Dockerfile.runner").is_file():
            return cand
    return None


#: None when `docker/` is absent, which only happens in an image that does
#: not ship it; both tests then skip rather than fail for want of a fixture.
ROOT = _root()
#: `services/terrapod/runner` in a checkout, `terrapod/runner` in the image.
RUNNER = (
    next(
        (
            p
            for p in (
                ROOT / "services" / "terrapod" / "runner",
                ROOT / "terrapod" / "runner",
            )
            if p.is_dir()
        ),
        None,
    )
    if ROOT
    else None
)
DOCKERFILE = (ROOT / "docker" / "Dockerfile.runner") if ROOT else None
TILTFILE = (ROOT / "Tiltfile") if ROOT else None

#: Entry point of the chain that runs inside a runner Job. None when the tree has
#: no runner package, which with ROOT is the whole skip condition — resolving it
#: eagerly is what turned a missing fixture into a COLLECTION error, taking the
#: entire shard down rather than skipping two tests.
ENTRYPOINT = (RUNNER / "job_entrypoint.py") if RUNNER else None

#: Modules the Job never imports, so the image is right not to carry them.
#: Each needs a reason: this list is how a genuine exclusion is told apart from
#: the omission this test exists to catch.
NOT_IN_THE_JOB = {
    "listener": "runs in the listener Deployment, not the Job",
    "identity": "listener-side join flow",
    "job_manager": "listener-side K8s client",
    "job_template": "listener-side Job construction",
    "__main__": "listener entry point",
}


#: Non-runner `terrapod.*` modules the Job imports, filled by the walk below.
#: Separate from the runner set because they live elsewhere in the tree and are
#: COPIED by their own lines in the Dockerfile.
_OTHER_TERRAPOD: set[str] = set()


def _runner_imports_reachable_from(start: pathlib.Path) -> set[str]:
    """Every `terrapod.runner.*` module reachable from `start`, transitively.

    Also records every OTHER `terrapod.*` module seen on the way, in
    `_OTHER_TERRAPOD`. That second half exists because the first version of this
    test followed `terrapod.runner.*` only, so a phase importing
    `terrapod.logging_config` -- which the image does not ship -- was invisible
    to it. Every runner Job died at import and this gate stayed green.
    """
    _OTHER_TERRAPOD.clear()
    seen: set[str] = set()
    queue = [start]
    while queue:
        f = queue.pop()
        if not f.exists():
            continue
        tree = ast.parse(f.read_text(), filename=str(f))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("terrapod.") and not node.module.startswith(
                    "terrapod.runner"
                ):
                    _OTHER_TERRAPOD.add(node.module.removeprefix("terrapod."))
                if node.module.startswith("terrapod.runner"):
                    tail = node.module.removeprefix("terrapod.runner").lstrip(".")
                    # Both the module and each imported NAME, because a name may
                    # itself be a submodule: `from terrapod.runner.phases import
                    # init_phase` reaches `phases/init_phase.py`, and recording
                    # only `phases` stops the walk at an empty `__init__.py` —
                    # which is how the first version of this test passed with the
                    # COPY line deleted. A name that is only a symbol resolves to
                    # no file and is skipped harmlessly.
                    if tail:
                        mods.append(tail)
                        mods += [f"{tail}/{a.name}" for a in node.names]
                    else:
                        mods += [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("terrapod.runner."):
                        mods.append(a.name.removeprefix("terrapod.runner."))
                    elif a.name.startswith("terrapod."):
                        _OTHER_TERRAPOD.add(a.name.removeprefix("terrapod."))
            for m in mods:
                name = m.replace(".", "/")
                if name in seen:
                    continue
                seen.add(name)
                queue.append(RUNNER / f"{name}.py")
                queue.append(RUNNER / name / "__init__.py")
    return seen


def test_every_module_the_job_imports_is_in_the_runner_image() -> None:
    if ROOT is None or RUNNER is None or ENTRYPOINT is None:
        pytest.skip("this tree has no docker/ or no runner package to check")
    dockerfile = DOCKERFILE.read_text()
    missing = []
    for mod in sorted(_runner_imports_reachable_from(ENTRYPOINT)):
        # A name in `from ... import x` may be a symbol rather than a submodule;
        # only report things that are really modules, so the message names the
        # file to add and nothing else.
        if not (RUNNER / f"{mod}.py").exists() and not (RUNNER / mod).is_dir():
            continue
        top = mod.split("/")[0]
        if top in NOT_IN_THE_JOB:
            continue
        # satisfied by either an explicit file COPY or a whole-directory COPY
        if (
            f"runner/{mod}.py" in dockerfile
            or f"runner/{top}/" in dockerfile
            or f"runner/{top}.py" in dockerfile
        ):
            continue
        missing.append(mod)
    assert not missing, (
        "these modules are imported by the runner Job but never COPIED into the "
        f"image, so every Job would die at import: {missing}. Add a COPY line to "
        "docker/Dockerfile.runner (and the path to the Tiltfile's "
        "build-runner-image deps)."
    )


def test_every_other_terrapod_module_the_job_imports_is_in_the_runner_image() -> None:
    """The same check for `terrapod.*` modules that are NOT under `runner/`.

    The Job legitimately imports a few modules from elsewhere in the tree, and
    the Dockerfile COPIES each by its own line. Nothing checked that, so adding
    one import was enough to break every Job: a phase took
    `terrapod.logging_config`, which the image does not ship, and the sibling
    test above could not see it because it follows `terrapod.runner.*` only.
    The runner-side convention is `structlog.get_logger(...)` directly, which
    `debug_linger` spells out and every phase but that one already followed.

    Judged on whether the module is COPIED, not on whether it exists -- the
    whole point is that it exists in the tree the tests import from.
    """
    if ROOT is None or RUNNER is None or ENTRYPOINT is None:
        pytest.skip("this tree has no docker/ or no runner package to check")
    dockerfile = DOCKERFILE.read_text()

    # Populates _OTHER_TERRAPOD as a side effect of the same walk.
    _runner_imports_reachable_from(ENTRYPOINT)
    assert _OTHER_TERRAPOD, (
        "the walk found no non-runner terrapod imports at all, which cannot be "
        "right -- the Job imports terrapod.http_retry among others. The walk has "
        "stopped collecting them, so this gate is inert; fix it rather than "
        "deleting it."
    )

    missing = []
    for mod in sorted(_OTHER_TERRAPOD):
        path = mod.replace(".", "/")
        # A `from terrapod.x import y` where y is a symbol, not a submodule.
        src = RUNNER.parent
        if not (src / f"{path}.py").exists() and not (src / path).is_dir():
            continue
        # An ancestor directory COPY ships it too: the Dockerfile takes
        # `services/terrapod/services/cost/` wholesale, so `services.cost.x` is
        # present without a line of its own. Checking only the exact path made
        # this gate report `terrapod.services.cost.pricesheet_db` as missing
        # when it ships -- a gate that is too wide gets weakened or deleted
        # rather than fixed, so it checks every prefix.
        parts = path.split("/")
        shipped = f"terrapod/{path}.py" in dockerfile or f"terrapod/{path}/" in dockerfile
        for i in range(1, len(parts)):
            if f"terrapod/{'/'.join(parts[:i])}/" in dockerfile:
                shipped = True
                break
        if shipped:
            continue
        missing.append(f"terrapod.{mod}")

    assert not missing, (
        "these non-runner modules are imported by the runner Job but never "
        f"COPIED into the image, so every Job would die at import: {missing}. "
        "Either COPY them in docker/Dockerfile.runner, or -- for logging, which "
        "is the usual case -- use `structlog.get_logger(...)` directly as every "
        "phase and `debug_linger` do."
    )


def test_the_tiltfile_deps_match_the_dockerfile() -> None:
    """Otherwise a local change to the module never rebuilds the image.

    Skipped in the test image, which does not copy the `Tiltfile` — it is a local
    development concern. It runs in a checkout, which is where someone adds a
    runner module in the first place.
    """
    if ROOT is None or not TILTFILE.is_file():
        pytest.skip("Tiltfile is not shipped in the test image")
    dockerfile = DOCKERFILE.read_text()
    tiltfile = TILTFILE.read_text()
    copied = {
        line.split()[1].removeprefix("services/terrapod/runner/")
        for line in dockerfile.splitlines()
        if line.startswith("COPY services/terrapod/runner/")
    }
    missing = sorted(m for m in copied if m.endswith(".py") and f"runner/{m}" not in tiltfile)
    assert not missing, (
        "COPIED into the runner image but absent from the Tiltfile's "
        f"build-runner-image deps, so editing it will not rebuild: {missing}"
    )


def test_the_test_image_ships_the_dockerfiles_this_test_reads() -> None:
    """Otherwise the guard above skips, silently, in CI.

    `ROOT` is None when `docker/` is absent and both tests then skip — which is
    indistinguishable from passing. The release lines' `Dockerfile.test` did not
    copy `docker/` in, so the guard could not run at all; restoring that COPY is
    what makes it a gate rather than a local convenience. Nothing pinned it.
    """
    if ROOT is None:
        pytest.skip("no docker/ in this tree")
    test_image = ROOT / "docker" / "Dockerfile.test"
    if not test_image.is_file():
        pytest.skip("Dockerfile.test is not shipped here")
    assert "COPY docker" in test_image.read_text(), (
        "docker/Dockerfile.test no longer copies `docker/` into the test image, so "
        "test_every_module_the_job_imports_is_in_the_runner_image SKIPS in CI and "
        "the runner-image omission it exists to catch ships unnoticed"
    )
