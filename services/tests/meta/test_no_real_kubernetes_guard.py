"""The Kubernetes guard covers every door, and cannot be deleted quietly (#2406).

`tests/conftest.py`'s `_no_real_kubernetes` exists because a test that forgets to
patch a Kubernetes call passes on a workstation with a kubeconfig and fails on a
CI runner without one. Two tests in #2399 did exactly that.

A guard like that is itself a thing that can rot, in three ways this pins:

* it can be deleted, and nothing would notice -- the suite goes green either way,
  which is the whole shape of the defect;
* it can be OUTGROWN. The first version refused `job_manager._get_batch_api`
  alone. `_get_core_api` was wide open, and it is the more dangerous of the two:
  `listener._create_auth_secret` / `_create_vars_secret` / `_create_ca_secret` and
  `identity.py`'s `create_namespaced_secret` / `replace_namespaced_secret` all go
  through it, so an unpatched test on a workstation pointed at a live cluster
  could overwrite a listener's credential Secret. A guard derived from the one
  symbol that happened to fail is the recorded "a derived gate's derivation
  decides its blind spot" shape;
* it can be bypassed entirely. `component_status._sample_blocking` builds its own
  `CoreV1Api`, `AppsV1Api` and `PolicyV1Api` after its own `load_kube_config()`
  and never touches `job_manager`, so no accessor-shaped guard can see it.

So the subjects are DERIVED rather than listed: every `_get_*_api` in
`job_manager`, and every config loader any client must call first. An accessor
added tomorrow is covered by the first; a client built somewhere new is covered by
the second, because a Kubernetes client cannot be built without loading config.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CONFTEST = pathlib.Path(__file__).resolve().parents[1] / "conftest.py"
JOB_MANAGER = pathlib.Path(__file__).resolve().parents[2] / "terrapod/runner/job_manager.py"


def _accessors() -> set[str]:
    """Every lazy client accessor `job_manager` defines, read from its source."""
    tree = ast.parse(JOB_MANAGER.read_text())
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("_get_")
        and node.name.endswith("_api")
    }


def _guard() -> ast.FunctionDef:
    # Matched first and returned once, rather than returning from inside the
    # loop and falling through to `pytest.fail`. `pytest.fail` is `NoReturn`,
    # so ruff is right that the old shape could not fall out of the function --
    # but CodeQL does not model `NoReturn` and read the fall-through as a
    # mixed return (py/mixed-returns). One explicit return says the same thing
    # in a form both analysers can check.
    found = [
        node
        for node in ast.parse(CONFTEST.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "_no_real_kubernetes"
    ]
    if not found:
        pytest.fail(
            "tests/conftest.py no longer defines the `_no_real_kubernetes` autouse fixture. "
            "Without it a test that forgets to patch a Kubernetes call passes on a machine "
            "with a kubeconfig and fails in CI, silently and by environment (#2406)."
        )
    return found[0]


def _cleared_globals() -> set[str]:
    """Module globals the fixture sets to None on `job_manager`.

    The `None` matters. "Any assignment to `job_manager.X`" is satisfied by the
    fixture's own RESTORE line -- `job_manager._batch_v1, job_manager._core_v1 =
    saved_clients` -- so a version of this helper that only looked at the target
    passed after the clearing was deleted, because the restore still named both.
    Clearing is what the test is about; restoring is the opposite operation.
    """
    out: set[str] = set()
    for node in ast.walk(_guard()):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and node.value.value is None):
            continue
        for target in node.targets:
            parts = list(target.elts) if isinstance(target, ast.Tuple) else [target]
            for part in parts:
                if (
                    isinstance(part, ast.Attribute)
                    and isinstance(part.value, ast.Name)
                    and part.value.id == "job_manager"
                ):
                    out.add(part.attr)
    return out


def test_the_guard_is_autouse() -> None:
    decorators = [ast.unparse(decorator) for decorator in _guard().decorator_list]
    assert any("autouse=True" in decorator for decorator in decorators), (
        f"`_no_real_kubernetes` carries {decorators} rather than `autouse=True`, so it "
        "protects only the tests that ask for it -- which are exactly the tests that "
        "already knew to patch."
    )


def _refusing_sentinel():
    """The single function the fixture installs on every door it closes.

    Taken from a loader rather than named: the fixture builds one `_refuse`
    closure and `setattr`s it everywhere, so identity with it is what says a
    particular door was closed. `load_incluster_config` is the reference because
    `test_the_guard_refuses_config_loading...` asserts independently that it
    refuses, so if this reference were ever wrong that test says so first.
    """
    from kubernetes import config as k8s_config

    return k8s_config.load_incluster_config


def test_the_guard_refuses_every_client_accessor_job_manager_defines() -> None:
    """RUN the accessors, and check IDENTITY -- the raise alone is masked (#2425 §2.1).

    The previous version parsed the fixture for string literals in any tuple or
    list and never tied a name to its `setattr`. So replacing the
    `for name in saved_accessors: setattr(...)` loop with one explicit
    `setattr(job_manager, "_get_batch_api", _refuse)` left the tuple literal
    standing and the test green -- with `_get_core_api` wide open, which is the
    accessor behind `listener`'s three `create_namespaced_secret` calls and
    `identity`'s `replace_namespaced_secret`. An unpatched test on a workstation
    pointed at a live cluster could overwrite a listener's credential Secret.

    **Calling it is not enough on its own, and that is the interesting part.**
    An unreplaced `_get_core_api` still raises here, because it loads config
    first and the loader guard refuses THAT -- so `pytest.raises` passes under
    the very mutation this test exists to catch. Defence in depth doing its job
    is what makes the outer door's absence invisible.

    So both: the call proves the object refuses, and identity with the fixture's
    one `_refuse` closure proves this particular door is the one that was closed
    rather than a deeper one catching the fall. Deriving the accessor list from
    `job_manager`'s own source means an accessor added tomorrow is covered with
    no edit here.
    """
    from terrapod.runner import job_manager

    accessors = _accessors()
    assert len(accessors) >= 2, (
        f"found only {accessors} in job_manager; the reader for `_get_*_api` has stopped "
        "reading, so this test is asserting nothing."
    )
    refuse = _refusing_sentinel()
    for name in sorted(accessors):
        door = getattr(job_manager, name)
        assert door is refuse, (
            f"`job_manager.{name}` is not the guard's refusing function, so that door is "
            "open. It may still raise -- config loading is refused underneath it -- which "
            "is exactly why this asserts identity. The accessor the guard forgot the "
            "first time was `_get_core_api`, the one that WRITES Secrets."
        )
        with pytest.raises(RuntimeError, match="reached the real Kubernetes client"):
            door()


def test_the_guard_refuses_config_loading_so_a_new_client_is_covered_too() -> None:
    """Also run, and this is the clause that closes the CLASS.

    `component_status._sample_blocking` builds its own `CoreV1Api`, `AppsV1Api`
    and `PolicyV1Api` after its own `load_kube_config()` and never goes through
    `job_manager`, so refusing the accessors alone cannot see it. A Kubernetes
    client cannot be built without loading config first, which is what makes this
    cover a client written somewhere new.
    """
    from kubernetes import config as k8s_config

    for name in ("load_incluster_config", "load_kube_config"):
        with pytest.raises(RuntimeError, match="reached the real Kubernetes client"):
            getattr(k8s_config, name)()


def test_no_module_imports_a_config_loader_BY_NAME() -> None:
    """A by-name import evades the fixture entirely.

    The guard replaces `kubernetes.config.load_kube_config` as a module
    ATTRIBUTE, so `from kubernetes import config` then `config.load_kube_config()`
    is covered -- the lookup happens at call time, through the patched module.
    `from kubernetes.config import load_kube_config` is not: it binds the real
    function into the importing module at import time, before any fixture runs,
    and nothing the guard does afterwards can reach that binding.

    Nothing does this today, so it is preventative rather than a fix -- and worth
    having precisely because the evasion is silent: the offending module would
    reach a real cluster while every other test in this file still passed.
    """
    import ast as _ast

    root = pathlib.Path(__file__).resolve().parents[2] / "terrapod"
    loaders = {"load_incluster_config", "load_kube_config"}
    offenders: list[str] = []
    scanned = 0
    for path in sorted(root.rglob("*.py")):
        scanned += 1
        for node in _ast.walk(_ast.parse(path.read_text())):
            if not isinstance(node, _ast.ImportFrom):
                continue
            if not (node.module or "").startswith("kubernetes"):
                continue
            for alias in node.names:
                if alias.name in loaders:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno} {alias.name}")
    # A floor, because a walk that stops walking must fail rather than pass on
    # nothing -- this repository's most repeated gate defect.
    assert scanned >= 100, f"only scanned {scanned} modules; the walk is in the wrong place"
    assert not offenders, (
        "these import a Kubernetes config loader by name, which binds the real function at "
        "import time and cannot be patched by `_no_real_kubernetes`:\n  "
        + "\n  ".join(offenders)
        + "\nImport the module instead -- `from kubernetes import config` -- and call "
        "`config.load_kube_config()`, so the lookup goes through the patched attribute."
    )


def test_the_guard_clears_the_cached_clients() -> None:
    missing = sorted({"_batch_v1", "_core_v1"} - _cleared_globals())
    assert not missing, (
        f"the guard never assigns `job_manager.{missing}`. `init_k8s()` populates both, "
        "so one unguarded route leaves a live client in a module global for the rest of "
        "the worker process, and restoring the accessor hands it straight back."
    )
