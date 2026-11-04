"""A URL in a RESPONSE BODY must not hardcode the deprecated `/api/v2` alias.

`/api/tfe/v2` is canonical and `/api/v2` is an alias kept for the support window
(#1529, sunset in `docs/deprecations.md`). An alias rescues an INBOUND request;
it does nothing for a URL we hand a client to follow, so every such URL on the
deprecated prefix becomes a 404 the day the alias is dropped -- a latent, dated
break rather than a live one, which is exactly the kind that ships.

`tfe_v2._state_version_json` is why this is a derived gate rather than a fixed
list: its `hosted-state-*-url` values already used `TFE_PREFIX`, with a comment
arguing that these URLs "must move with the routes, or state upload silently
keeps using the old path" -- and `links.self`/`links.download` ten lines below
still said `/api/v2`. One function, the right reasoning written down, half
applied. A survey found it; nothing in CI could.

Scoped to string literals the routers evaluate, so a docstring or comment may
still discuss the alias (the alias has to be documented somewhere).
"""

from __future__ import annotations

import ast
import pathlib

from terrapod.api.prefixes import TFE_LEGACY_PREFIX

ROUTERS = sorted(
    (pathlib.Path(__file__).resolve().parents[2] / "terrapod" / "api" / "routers").glob("*.py")
)

#: Floors, so a matcher that stops matching fails here rather than passing on
#: nothing. Both are well below the real counts.
MIN_ROUTERS = 15
MIN_STRINGS = 500


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of Constant nodes that are docstrings, which this gate ignores."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.add(id(first.value))
    return out


def _evaluated_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every string literal the module EVALUATES, with its line number.

    Includes the constant segments of an f-string: a URL is almost always built
    as `f"{base}{PREFIX}/thing/{id}"`, so the prefix lives in a `JoinedStr`
    part rather than in a plain `Constant`.
    """
    tree = ast.parse(path.read_text())
    skip = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            found.append((node.lineno, node.value))
    return found


def test_no_response_url_hardcodes_the_deprecated_prefix() -> None:
    assert len(ROUTERS) >= MIN_ROUTERS, (
        f"only {len(ROUTERS)} router modules found, below the floor of {MIN_ROUTERS}; "
        "the glob has stopped matching -- fix it rather than lowering the floor"
    )

    inspected = 0
    offenders: list[str] = []
    for path in ROUTERS:
        for lineno, value in _evaluated_strings(path):
            inspected += 1
            if TFE_LEGACY_PREFIX in value:
                offenders.append(f"{path.name}:{lineno}: {value!r}")

    assert inspected >= MIN_STRINGS, (
        f"only {inspected} evaluated string literals inspected, below the floor of "
        f"{MIN_STRINGS}; the AST walk has stopped finding them"
    )

    assert not offenders, (
        "a URL handed to a client is on the deprecated `/api/v2` alias and will 404 "
        "when the alias is dropped. Use `TFE_PREFIX` (or derive the surface from the "
        "request with `is_tfe_path`) instead:\n  " + "\n  ".join(offenders)
    )
