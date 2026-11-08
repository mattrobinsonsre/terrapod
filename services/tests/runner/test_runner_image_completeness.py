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
import importlib.metadata
import pathlib
import re
import sys

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


# Non-runner modules are copied separately into the runner image.
_OTHER_TERRAPOD: set[str] = set()


def _runner_imports_reachable_from(start: pathlib.Path) -> set[str]:
    """Collect reachable runner modules and imports outside `terrapod.runner`."""
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


def _fixtures_or_fail(*needed: tuple[object, str]) -> None:
    """Refuse, rather than skip, when a fixture this file reads is missing.

    Thirteen `pytest.skip` guards used to stand here, eight of them saying the
    `Tiltfile` "is not shipped in the test image". **It is** --
    `docker/Dockerfile.test` carries `COPY Tiltfile ./Tiltfile`, and it copies
    `docker/` twice -- so not one of those conditions can arise in either layout,
    and the only way to reach them is for somebody to rename or delete the file.
    At which point all thirteen went quiet together, including "every
    third-party module the Job imports is installed in the image" (#2425 §2.6).

    A skip is right for a fixture that is legitimately absent somewhere. These
    are present in both layouts by construction, so absence is a defect in the
    tree and has to read as one.
    """
    missing = [what for value, what in needed if value is None or not _present(value)]
    if missing:
        pytest.fail(
            "this file reads fixtures that are shipped in BOTH layouts -- a checkout and "
            f"the test image -- and these are missing: {', '.join(missing)}. "
            "`docker/Dockerfile.test` copies `docker/` and `Tiltfile`, so this is not an "
            "image that omits them: something in the tree has been renamed or removed, "
            "and skipping would take every test in this file with it."
        )


def _present(value: object) -> bool:
    """True for a path that exists, and for any non-path sentinel."""
    return value.exists() if isinstance(value, pathlib.Path) else True


def test_every_module_the_job_imports_is_in_the_runner_image() -> None:
    _fixtures_or_fail(
        (ROOT, "docker/"), (RUNNER, "the runner package"), (ENTRYPOINT, "the Job entrypoint")
    )
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
    """Non-runner imports must also be present in the image's COPY list."""
    _fixtures_or_fail(
        (ROOT, "docker/"), (RUNNER, "the runner package"), (ENTRYPOINT, "the Job entrypoint")
    )
    dockerfile = DOCKERFILE.read_text()
    _runner_imports_reachable_from(ENTRYPOINT)
    assert _OTHER_TERRAPOD, (
        "the walk found no non-runner terrapod imports at all, which cannot be "
        "right -- the Job imports terrapod.http_retry among others"
    )

    missing = []
    for mod in sorted(_OTHER_TERRAPOD):
        path = mod.replace(".", "/")
        src = RUNNER.parent
        if not (src / f"{path}.py").exists() and not (src / path).is_dir():
            continue
        parts = path.split("/")
        shipped = f"terrapod/{path}.py" in dockerfile or f"terrapod/{path}/" in dockerfile
        for i in range(1, len(parts)):
            if f"terrapod/{'/'.join(parts[:i])}/" in dockerfile:
                shipped = True
                break
        if not shipped:
            missing.append(f"terrapod.{mod}")

    assert not missing, (
        "these non-runner modules are imported by the runner Job but never "
        f"COPIED into the image: {missing}"
    )


def test_the_tiltfile_deps_match_the_dockerfile() -> None:
    """Otherwise a local change to the module never rebuilds the image.

    The test image carries the Tiltfile so this also guards the dev image in CI.
    """
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
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


def test_kind_runner_image_is_loaded_before_jobs_start() -> None:
    """A host-only build fails with ErrImageNeverPull inside kind (#1990)."""
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
    tiltfile = TILTFILE.read_text()
    assert "_kind = k8s_context() == 'kind-kind'" in tiltfile
    assert (
        "elif _kind:\n    _RUNNER_BUILD = _RUNNER_BUILD + ' && kind load docker-image ' "
        "+ _RUNNER_TAG + ' --name kind'"
    ) in tiltfile
    assert "cmd=QUERY_BUILD + ' && ' + EE_AGENT_BUILD + _RUNNER_BUILD" in tiltfile


def test_tilt_go_images_meet_module_minimum_patch_versions() -> None:
    """A floating Go image tag may lag behind the modules' required patch."""
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
    tiltfile = TILTFILE.read_text()
    for build, module in (("QUERY_BUILD", "query"), ("EE_AGENT_BUILD", "ee-agent")):
        go_mod = ROOT / module / "go.mod"
        assert go_mod.is_file(), f"{module}/go.mod must be shipped in the test image"
        required = re.search(r"^go (\d+)\.(\d+)\.(\d+)$", go_mod.read_text(), re.MULTILINE)
        assert required is not None, f"{module}/go.mod must specify a Go patch version"
        command = tiltfile.split(f"{build} = (", 1)[1].split("\n)", 1)[0]
        pinned = re.search(r"golang:(\d+)\.(\d+)\.(\d+)\b", command)
        assert pinned is not None, f"{build} must pin a Go image with a patch version"
        assert tuple(map(int, pinned.groups())) >= tuple(map(int, required.groups())), (
            f"{build} uses Go {pinned.group(0)}, below {module}/go.mod's minimum "
            f"{required.group(0)}"
        )


def test_tilt_go_builds_do_not_write_into_watched_source() -> None:
    """A build output inside ee-agent/ retriggers its own local_resource forever."""
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
    tiltfile = TILTFILE.read_text()
    for build, source, output in (
        ("QUERY_BUILD", "query", "query-bin/terrapod-query"),
        ("EE_AGENT_BUILD", "ee-agent", "ee-agent-bin/terrapod-ee-agent"),
    ):
        command = tiltfile.split(f"{build} = (", 1)[1].split("\n)", 1)[0]
        output_dir, filename = output.split("/", 1)
        assert f'-v "$PWD/{source}":/src:ro' in command
        assert f'-v "$PWD/docker/{output_dir}":/out' in command
        assert f"go build -trimpath -o /out/{filename}" in command


def test_tilt_builds_provider_from_local_git_tags_without_watching_its_output() -> None:
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
    tiltfile = TILTFILE.read_text()
    assert "str(local(\"git tag --list 'v[0-9]*' --sort=-version:refname\"))" in tiltfile
    assert "_PROVIDER_TAG = _PROVIDER_TAGS.split('\\n')[0]" in tiltfile
    assert "gh api repos/mattrobinsonsre/terrapod/releases/latest" not in tiltfile
    provider_build = tiltfile.split("'build-provider-binary',", 1)[1].split("labels=['build'],", 1)[
        0
    ]
    assert (
        "cmd='make -C provider dev-package BASE_VERSION=' + shlex.quote(_PROVIDER_TAG[1:])"
        in provider_build
    )
    for dependency in (
        "provider/GNUmakefile",
        "provider/dev-package.sh",
        "provider/main.go",
        "provider/internal",
        "go-terrapod",
    ):
        assert f"'{dependency}'" in provider_build
    assert "'provider'" not in provider_build.split("deps=[", 1)[1]
    assert "api.devProviderVersion=' + _PROVIDER_VERSION" in tiltfile
    assert "cmd='bash scripts/dev-provider-stage.sh ' + shlex.quote(_PROVIDER_VERSION)" in tiltfile
    api_resource = tiltfile.split("k8s_resource(\n    'terrapod-api',", 1)[1].split("\n)", 1)[0]
    assert "'seed-dev-provider-cache'" in api_resource


def test_tilt_down_does_not_run_local_setup_or_builds() -> None:
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
    tree = ast.parse(TILTFILE.read_text())
    guarded = []
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            assert (
                not isinstance(statement.value.func, ast.Name) or statement.value.func.id != "local"
            )
        if (
            isinstance(statement, ast.If)
            and ast.unparse(statement.test) == "config.tilt_subcommand != 'down'"
        ):
            guarded.extend(
                ast.unparse(child.value.args[0])
                for child in statement.body
                if isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Call)
                and isinstance(child.value.func, ast.Name)
                and child.value.func.id == "local"
            )
    assert len(guarded) == 4
    assert any("kubectl create namespace terrapod" in command for command in guarded)
    assert "QUERY_BUILD" in guarded
    assert "EE_AGENT_BUILD" in guarded
    assert any("make -C provider dev-package" in command for command in guarded)


def test_existing_dev_certificates_do_not_require_mkcert() -> None:
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
    setup = (
        TILTFILE.read_text().split("'setup-certificates',", 1)[1].split("labels=['setup'],", 1)[0]
    )
    assert "if command -v mkcert &> /dev/null; then" in setup
    assert 'elif [ ! -s "$CERT_DIR/cert.pem" ] || [ ! -s "$CERT_DIR/key.pem" ]; then' in setup


def test_seed_dev_provider_waits_for_storage_pvc_before_start() -> None:
    """The helper pod cannot mount a PVC Tilt has not applied yet (#1990)."""
    _fixtures_or_fail((ROOT, "docker/"), (TILTFILE, "Tiltfile"))
    tiltfile = TILTFILE.read_text()
    seed = tiltfile.split("'seed-dev-provider-cache',", 1)[1].split("labels=['setup'],", 1)[0]
    assert "resource_deps=['build-provider-binary', 'uncategorized']" in seed


def test_the_test_image_ships_the_dockerfiles_this_test_reads() -> None:
    """Otherwise the guard above skips, silently, in CI.

    `ROOT` is None when `docker/` is absent and both tests then skip — which is
    indistinguishable from passing. The release lines' `Dockerfile.test` did not
    copy `docker/` in, so the guard could not run at all; restoring that COPY is
    what makes it a gate rather than a local convenience. Nothing pinned it.
    """
    _fixtures_or_fail((ROOT, "docker/"))
    test_image = ROOT / "docker" / "Dockerfile.test"
    _fixtures_or_fail((test_image, "docker/Dockerfile.test"))
    assert "COPY docker" in test_image.read_text(), (
        "docker/Dockerfile.test no longer copies `docker/` into the test image, so "
        "test_every_module_the_job_imports_is_in_the_runner_image SKIPS in CI and "
        "the runner-image omission it exists to catch ships unnoticed"
    )
    assert "COPY Tiltfile" in test_image.read_text() and TILTFILE.is_file(), (
        "docker/Dockerfile.test must ship Tiltfile, or the kind runner-image "
        "delivery guard silently skips in CI"
    )


# Third-party dependencies are the other half of runner-image completeness.
#
# TWO LAYOUTS, like `RUNNER` above, and this one was missing its second
# candidate. `docker/Dockerfile.test` copies the three requirements files to
# `./` -- so in the image the file is at `/app/requirements-runner.txt` while
# `ROOT / "services" / ...` resolves to `/app/services/...`, which is not there.
#
# The consequence, which is what #2425 §2.6 was really about: the guard below
# carried `pytest.skip("no docker/ or requirements-runner.txt in this tree")`,
# so **"every third-party module the Job imports is installed in the image" had
# never once run in CI**. It passed in a checkout, skipped in the image, and the
# skip's stated reason was true -- the file genuinely was not where it looked.
# Turning the skip into a failure is what surfaced it.
REQUIREMENTS = (
    next(
        (
            p
            for p in (
                ROOT / "services" / "requirements-runner.txt",
                ROOT / "requirements-runner.txt",
            )
            if p.is_file()
        ),
        None,
    )
    if ROOT
    else None
)


def _terrapod_package_root() -> pathlib.Path | None:
    """`services/terrapod` in a checkout, `terrapod` in the image."""
    if ROOT is None:
        return None
    for cand in (ROOT / "services" / "terrapod", ROOT / "terrapod"):
        if cand.is_dir():
            return cand
    return None


def _third_party_imports_reachable_from(start: pathlib.Path) -> dict[str, str]:
    """Top-level non-stdlib imports from all modules the runner image ships."""
    pkg = _terrapod_package_root()
    if pkg is None:
        return {}
    found: dict[str, str] = {}
    seen: set[pathlib.Path] = set()
    queue = [start]
    while queue:
        f = queue.pop()
        if f in seen or not f.exists():
            continue
        seen.add(f)
        tree = ast.parse(f.read_text(), filename=str(f))
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            submodule_names = (
                [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else []
            )
            for dotted in names:
                top = dotted.split(".")[0]
                if top == "terrapod":
                    rel = dotted.removeprefix("terrapod.").replace(".", "/")
                    queue.append(pkg / f"{rel}.py")
                    queue.append(pkg / rel / "__init__.py")
                    for n in submodule_names:
                        queue.append(pkg / rel / f"{n}.py")
                        queue.append(pkg / rel / n / "__init__.py")
                elif top not in sys.stdlib_module_names and top != "__future__":
                    found.setdefault(top, f.name)
    return found


def test_every_third_party_module_the_job_imports_is_installed_in_the_image() -> None:
    """Catch transitive imports missing from the runner's pinned requirements."""
    _fixtures_or_fail(
        (ROOT, "docker/"),
        (ENTRYPOINT, "the Job entrypoint"),
        (REQUIREMENTS, "requirements-runner.txt"),
    )

    pinned = {
        line.split("==")[0].strip().lower().replace("_", "-")
        for line in REQUIREMENTS.read_text().splitlines()
        if "==" in line and not line.lstrip().startswith("#")
    }
    assert len(pinned) >= 10, (
        f"only {len(pinned)} pinned package(s) parsed out of {REQUIREMENTS.name} -- "
        "the matcher has stopped matching, so this gate would pass on anything"
    )

    imports = _third_party_imports_reachable_from(ENTRYPOINT)
    assert len(imports) >= 3, (
        f"only {len(imports)} third-party import(s) found from {ENTRYPOINT.name} -- "
        "the walk has stopped walking, so this gate would pass on anything"
    )

    mapping = importlib.metadata.packages_distributions()
    problems: list[str] = []
    for module, importer in sorted(imports.items()):
        dists = mapping.get(module)
        if not dists:
            problems.append(
                f"{module} (imported by {importer}) resolves to no installed "
                "distribution, so this gate cannot verify it is in the image"
            )
            continue
        if not any(d.lower().replace("_", "-") in pinned for d in dists):
            problems.append(
                f"{module} (imported by {importer}) comes from {dists}, none of "
                f"which is pinned in {REQUIREMENTS.name} -- every runner Job will "
                f"die at import with ModuleNotFoundError: No module named '{module}'"
            )
    assert not problems, "\n".join(problems)
