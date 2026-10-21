"""The API's resolved-inventory artifact carries NO credentials (#1933).

A requirement stated outright: the inventory shipped from the API must not have
the secrets already in it. The credential merge is a step on the RUNNER.

That is how it is built -- the API resolves the inventory from the inventory
rows at run creation, and the runner merges the machine credential's connection
variables into its own local render afterwards -- and the point of this file is
that it stays built that way. The tempting "simplification" is to inject
server-side, where the variables are already resolved and the document is
already being written: it would be fewer moving parts and it would put a
connection password into object storage, into the Resolved view, and into
anything holding inventory read.

So these are structural gates over the API's artifact path rather than
behavioural tests. The behaviour is pinned in
`tests/runner/test_configure_inventory.py`, which asserts the downloaded
document and the rendered file differ by exactly the credential.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from terrapod.services import inventory_resolve

#: The modules and names that mean "a credential". A reference to any of them
#: inside the API's artifact path is the defect.
_CREDENTIAL_MARKERS = (
    "ansible_auth",
    "delivery_entries",
    "injections",
    "ansible_ssh_private_key_file",
    "ansible_become_password",
)

#: The API-side functions that produce the stored artifact.
_ARTIFACT_PATH = ("resolve_for_run", "write_run_artifact", "render_inventory_yaml")


def _function(name: str) -> ast.AST:
    tree = ast.parse(pathlib.Path(inventory_resolve.__file__).read_text())
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
    ]
    assert len(found) == 1, f"{name} was renamed or duplicated; fix this gate"
    return found[0]


@pytest.mark.parametrize("name", _ARTIFACT_PATH)
@pytest.mark.parametrize("marker", _CREDENTIAL_MARKERS)
def test_the_artifact_path_references_no_credential(name: str, marker: str) -> None:
    """Derived over the function's whole subtree, so a helper call added inside
    a branch is covered as well as a direct reference."""
    assert marker not in ast.dump(_function(name)), (
        f"{name} references {marker!r}. The API's resolved-inventory artifact must carry no "
        f"credential: it is stored in object storage, served to the Resolved view, and "
        f"readable by anyone holding inventory read. The merge belongs on the runner, in "
        f"`runner/phases/configure_inventory`, whose render is local to the Job."
    )


def test_the_module_imports_no_credential_machinery_at_all() -> None:
    """The stronger form: not even available to be called.

    Checked as well as the per-function gate because the cheapest wrong fix is a
    module-level import plus one line inside a function this gate does not
    name -- and `_ARTIFACT_PATH` is a list, which is the thing that can go stale.
    """
    tree = ast.parse(pathlib.Path(inventory_resolve.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    offending = {n for n in imported if "ansible_auth" in n}
    assert not offending, (
        f"{pathlib.Path(inventory_resolve.__file__).name} imports {sorted(offending)}. The "
        f"API side has no business holding the credential schema: it neither validates nor "
        f"delivers one, and importing it is the first half of injecting server-side."
    )


def test_the_only_caller_of_delivery_entries_is_the_secret_path() -> None:
    """One caller, and it is the per-run Secret rather than the artifact.

    Derived by grepping the tree rather than asserting a count, so a second
    caller names itself in the failure.
    """
    root = pathlib.Path(inventory_resolve.__file__).resolve().parents[1]
    callers = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "delivery_entries(" in p.read_text() and p.name != "ansible_auth.py"
    )
    assert callers == ["api/routers/runs.py"], (
        f"delivery_entries is called from {callers}. Its one caller composes the per-run "
        f"Secret for a configure; anything else is a credential reaching a surface that "
        f"was not designed to hold one."
    )
