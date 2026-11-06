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
It lives in `tests/runner/` rather than a `tests/meta/` tier because this release
line has no such tier -- `tests/meta` is absent from the unit shard's path list in
`ci.yml`, so a test placed there would be collected by nothing and this guard
would be unprotected on the line while `main` has it protected. The paths below
are relative to `parents[1]`/`parents[2]`, which are the same from either
directory, so nothing else changes.
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
    for node in ast.parse(CONFTEST.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_no_real_kubernetes":
            return node
    pytest.fail(
        "tests/conftest.py no longer defines the `_no_real_kubernetes` autouse fixture. "
        "Without it a test that forgets to patch a Kubernetes call passes on a machine "
        "with a kubeconfig and fails in CI, silently and by environment (#2406)."
    )


def _replaced_names() -> set[str]:
    """The names the fixture actually swaps out.

    Read from the collection literals it ITERATES, never from its source text. A
    substring check over the body is satisfied by the refusal message, which names
    every accessor on purpose so the error can tell the author what to patch -- so
    `"_get_core_api" in source` stayed true after the guard stopped replacing it,
    and the first version of this test passed under exactly that mutation. "Does
    the source mention X" is the weak form of "does the code do X to X", and this
    is the file where that distinction is the entire subject.
    """
    out: set[str] = set()
    for node in ast.walk(_guard()):
        if isinstance(node, ast.Tuple | ast.List):
            out |= {
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    return out


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


def test_the_guard_refuses_every_client_accessor_job_manager_defines() -> None:
    accessors = _accessors()
    assert len(accessors) >= 2, (
        f"found only {accessors} in job_manager; the reader for `_get_*_api` has stopped "
        "reading, so this test is asserting nothing."
    )
    missing = sorted(accessors - _replaced_names())
    assert not missing, (
        f"`job_manager` defines {sorted(accessors)} and the guard replaces {missing} "
        "nowhere, so that door is open. The accessor it forgot the first time was "
        "`_get_core_api`, which is the one that WRITES Secrets -- `listener`'s three "
        "`create_namespaced_secret` calls and `identity`'s `replace_namespaced_secret` "
        "all go through it."
    )


def test_the_guard_refuses_config_loading_so_a_new_client_is_covered_too() -> None:
    missing = sorted({"load_incluster_config", "load_kube_config"} - _replaced_names())
    assert not missing, (
        f"the guard replaces {missing} nowhere. That is what closes the CLASS: "
        "`component_status._sample_blocking` builds its own `CoreV1Api`, `AppsV1Api` and "
        "`PolicyV1Api` after its own `load_kube_config()` and never goes through "
        "job_manager, so refusing the accessors alone cannot see it."
    )


def test_the_guard_clears_the_cached_clients() -> None:
    missing = sorted({"_batch_v1", "_core_v1"} - _cleared_globals())
    assert not missing, (
        f"the guard never assigns `job_manager.{missing}`. `init_k8s()` populates both, "
        "so one unguarded route leaves a live client in a module global for the rest of "
        "the worker process, and restoring the accessor hands it straight back."
    )
