"""The Vault file renderer stays pure (#1648).

A template is operator-supplied text stored in a variable and evaluated on the
API server. The renderer must be able to do nothing but place fields of the
secret: no filesystem, no process, no network, no evaluation. This reads the
module's source and fails if any of that creeps in.
"""

import ast
import inspect

from terrapod.services import vault_render

FORBIDDEN_MODULES = {"os", "subprocess", "httpx", "pathlib", "socket", "urllib", "shutil", "io"}
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "getattr", "globals", "locals"}


def _tree() -> ast.Module:
    return ast.parse(inspect.getsource(vault_render))


def test_it_imports_nothing_that_reaches_the_os_or_the_network():
    imported: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & FORBIDDEN_MODULES, imported & FORBIDDEN_MODULES
    # Nothing from Terrapod either: the renderer takes plain values in.
    assert "terrapod" not in imported


def test_it_never_calls_open_eval_or_exec():
    called = {
        node.func.id
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & FORBIDDEN_CALLS, called & FORBIDDEN_CALLS


def test_the_guard_would_notice_a_forbidden_import():
    """The check is live: the same walk finds an import in a sample."""
    sample = ast.parse("import os\nfrom urllib import parse\nopen('x')")
    found = {a.name for n in ast.walk(sample) if isinstance(n, ast.Import) for a in n.names} | {
        n.module for n in ast.walk(sample) if isinstance(n, ast.ImportFrom)
    }
    assert {"os", "urllib"} <= found
