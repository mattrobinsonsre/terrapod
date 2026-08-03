"""Optional arguments in `vcs_poller` are passed by keyword, not position.

This gate exists because of a total VCS outage in v1.3.0 (#1244).

#1097 added a per-cycle metadata cache — a coalescing *wrapper* around the VCS
provider calls, reached through an optional `meta` parameter. That grew the tail
of `_poll_workspace_branch` / `_poll_workspace_prs` to three defaulted
parameters, `(cache, meta, fetch_paths)`, and `_poll_workspace` passed them
positionally as `(cache, fetch_paths, meta)`. The `fetch_paths` list bound to
`meta`, the helper took its `meta is not None` branch, and every poll died on
`'list' object has no attribute 'get_or_fetch'`.

The deeper reason it shipped is worth stating, because it generalises past this
one call site. **An optional parameter that gates a wrapper makes the bypass the
default, and tests take defaults.** Every `_poll_workspace` test omitted `meta`,
so the helpers took the `meta is None` short-circuit and the wrapper body never
ran; the tests that did construct a `VCSMetadataCache` called the inner helpers
directly. Production was the only caller passing both — so the wrapper was never
once exercised through the poll cycle, and a transposition inside it was
invisible to a green suite.

Behavioural coverage for that now lives in
`TestPollWorkspacePassesMetadataCacheCorrectly`. This gate is the structural
half: it removes the failure mode itself rather than testing around it. Keywords
cannot transpose, so reordering or inserting a defaulted parameter can no longer
silently rebind an argument at a call site.

Scope is deliberately narrow — only calls, within this module, to functions
defined in this module that carry two or more defaulted parameters. One optional
argument cannot transpose, and cross-module calls are somebody else's contract.
"""

import ast
import inspect
from pathlib import Path

from terrapod.services import vcs_poller

# Two or more defaulted parameters is where positional order starts to be a
# hazard worth banning; with one there is nothing to swap it with.
MIN_DEFAULTS = 2


def _module_tree() -> ast.Module:
    return ast.parse(Path(inspect.getfile(vcs_poller)).read_text())


def _functions_with_optional_tails(tree: ast.Module) -> dict[str, int]:
    """name -> count of REQUIRED positional params, for at-risk functions."""
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        args = node.args
        n_defaults = len(args.defaults)
        if n_defaults >= MIN_DEFAULTS:
            out[node.name] = len(args.args) - n_defaults
    return out


def test_optional_args_are_passed_by_keyword():
    tree = _module_tree()
    at_risk = _functions_with_optional_tails(tree)
    assert at_risk, "expected vcs_poller to define functions with optional tails"

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name not in at_risk:
            continue
        required = at_risk[name]
        if len(node.args) > required:
            extra = len(node.args) - required
            violations.append(
                f"  line {node.lineno}: {name}(...) passes {extra} optional "
                f"argument(s) positionally; pass them by keyword"
            )

    assert not violations, (
        "Optional arguments passed positionally in vcs_poller.py.\n\n"
        "The tail of these signatures is a run of same-shaped optional values "
        "(a cache, a metadata cache, a path list). Passing them positionally is "
        "how #1244 transposed `fetch_paths` into `meta` and took VCS polling "
        "down entirely — and because the wrong value was still a plausible "
        "type, nothing failed until it was dereferenced in production.\n\n" + "\n".join(violations)
    )
