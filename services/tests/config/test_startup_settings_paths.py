"""Every `settings.…` path in the startup module must resolve (#1408).

`create_application`'s lifespan is where background tasks are registered, and it
is the one block of code that runs exactly once, at boot, in production only:
the integration suite swaps the lifespan out for a lighter one, so a typo in a
config path there is invisible to every test and surfaces as a crash-looping API
pod after deploy.

That is not hypothetical — it is why this file exists. A reaper was registered
behind `settings.oci.enabled` when the config actually lives at
`settings.registry.oci`, the whole suite stayed green, and the only thing that
would have caught it was starting the real app.

The check is deliberately static: it reads the attribute chains out of the AST
and resolves each one against a real `Settings`, so it costs nothing and cannot
be defeated by a branch that happens not to be taken.
"""

from __future__ import annotations

import ast
import pathlib

from terrapod.config import settings

APP = pathlib.Path(__file__).resolve().parents[2] / "terrapod" / "api" / "app.py"


def _settings_chains(tree: ast.AST) -> set[str]:
    """Every dotted path in the module rooted at the name `settings`."""
    found: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id == "settings":
            found.add(".".join(reversed(parts)))

    # Keep only the longest form of each chain: walking the AST yields
    # `registry.oci` as well as `registry.oci.enabled`, and resolving the leaf
    # covers its prefixes anyway.
    return {c for c in found if not any(o != c and o.startswith(c + ".") for o in found)}


def _resolve(chain: str) -> None:
    obj = settings
    for part in chain.split("."):
        obj = getattr(obj, part)


def test_every_settings_path_in_app_py_resolves() -> None:
    chains = _settings_chains(ast.parse(APP.read_text()))

    # A guard on the guard: if the extraction ever stops finding anything (a
    # refactor moves the lifespan, say), the test must fail rather than pass
    # vacuously by checking an empty set.
    assert len(chains) > 20, f"suspiciously few settings paths found in app.py: {chains}"

    unresolved = []
    for chain in sorted(chains):
        try:
            _resolve(chain)
        except AttributeError as exc:
            unresolved.append(f"settings.{chain} — {exc}")

    assert not unresolved, "startup references config that does not exist:\n" + "\n".join(
        unresolved
    )
