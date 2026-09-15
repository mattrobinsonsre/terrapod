"""Source invariants for Vault file delivery (#1619).

Behavioural tests prove today's code keeps a file's content out of the Job spec
and the logs. These pin the *shape* that makes it so, so a later edit that
routes a value somewhere it must never go fails CI loudly rather than in a
security review:

- the Job builder's file helper never reads a value from an entry, and
  `build_job_spec` never puts anything from `vault_files` into container env;
- no logger call in the listener, the resolver or `next_run` is handed a
  variable that holds file content, and the listener's refusal messages are
  built from names only.
"""

import ast
import inspect
import textwrap

from terrapod.api.routers import runs
from terrapod.runner import job_template, listener
from terrapod.services import vault_source_service


def _fn(module, name: str) -> ast.AST:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module.__name__}")


def _method(cls, name: str) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(getattr(cls, name)))).body[0]


def _parents(tree: ast.AST) -> dict:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _reads_key(node: ast.AST, key: str) -> bool:
    """`x["key"]` or `x.get("key", ...)` anywhere under node."""
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value == key
        ):
            return True
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == key
        ):
            return True
    return False


def _logger_calls(node: ast.AST) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "logger"
    ]


def _names_under(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


# ── Job spec ──────────────────────────────────────────────────────────


def test_the_file_mount_helper_never_reads_a_value():
    helper = _fn(job_template, "_add_vault_file_mounts")
    assert not _reads_key(helper, "value")
    assert not _reads_key(helper, "content")


def test_build_job_spec_hands_vault_files_only_to_the_mount_helper():
    fn = _fn(job_template, "build_job_spec")
    parents = _parents(fn)
    uses = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "vault_files"]
    assert uses, "the bite-check: build_job_spec must still take vault_files"
    for use in uses:
        node = use
        while node in parents:
            node = parents[node]
            # Never iterated here, and never inside an env append.
            assert not (isinstance(node, ast.For) and node.iter is use)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "container_env"
            ):
                raise AssertionError("vault_files reached container env in build_job_spec")


# ── Logs ──────────────────────────────────────────────────────────────

#: Names that hold, or iterate over, file content in the code paths below.
_VALUE_NAMES = {
    "vault_file_values",
    "values",
    "value",
    "content",
    "string_data",
    "secret",
    "vault",
    "out",
    "f",
    "raw",
}


def test_the_listener_never_logs_file_content():
    tree = ast.parse(inspect.getsource(listener))
    calls = _logger_calls(tree)
    assert len(calls) > 20, "bite-check: the extractor must see the listener's log calls"
    for call in calls:
        leaked = _names_under(call) & _VALUE_NAMES
        assert not leaked, f"logger call on line {call.lineno} is handed {sorted(leaked)}"
        assert not _reads_key(call, "value")


def test_the_listener_builds_refusals_from_names_only():
    fn = _method(listener.RunnerListener, "_plan_vault_files")
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert raises, "bite-check"
    for r in raises:
        assert not _reads_key(r, "value")
        assert not (_names_under(r) & {"values", "vault_file_values", "raw"})


def test_the_listener_hands_values_only_to_the_vars_secret():
    fn = _method(listener.RunnerListener, "_launch_run")
    parents = _parents(fn)
    uses = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "vault_file_values"]
    assert uses
    for use in uses:
        parent = parents[use]
        if isinstance(parent, ast.Tuple):  # the unpacking assignment
            continue
        assert isinstance(parent, ast.keyword) and parent.arg == "vault_file_values"
        call = parents[parent]
        assert isinstance(call.func, ast.Attribute) and call.func.attr == "_create_vars_secret"


def test_the_resolver_never_logs_a_value():
    calls = _logger_calls(ast.parse(inspect.getsource(vault_source_service)))
    assert calls, "bite-check"
    for call in calls:
        leaked = _names_under(call) & _VALUE_NAMES
        assert not leaked, f"logger call on line {call.lineno} is handed {sorted(leaked)}"


def test_next_run_never_logs_the_vault_delivery():
    fn = _fn(runs, "next_run")
    for call in _logger_calls(fn):
        assert not (_names_under(call) & _VALUE_NAMES)
