"""The Alembic revision graph stays one line, with every release line on it (#1594).

Release lines and `main` add migrations independently. When a release line is
carried forward, its migrations go into `main`'s chain at the point the line
branched, and `main`'s later migrations are re-parented on top of them — #1452
did exactly that for 1.6. Every release line's chain is then a prefix of
`main`'s, so a deployment on that line upgrades by running only the migrations
it has not seen.

Getting that wrong is silent. With two heads, `alembic upgrade head` refuses,
or runs branches in an order nobody reviewed. With a release head that is not
on the chain, a deployment on that line either finds its revision unknown or
treats later migrations as applied and skips them, and the code then runs
against a schema missing what it expects.

So, on every branch:

1. there is exactly one root and one head, and no revision has two parents;
2. every parent a revision names exists, and no revision is defined twice;
3. every release head in `alembic_release_heads.json` is an ancestor of the
   head, or the head itself.

The scripts are read statically, as `test_migration_contract.py` reads them.
No database is involved.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests.db.test_migration_contract import _migration_files

_RELEASE_HEADS = Path(__file__).parent / "alembic_release_heads.json"

Graph = dict[str, tuple[str, ...]]


def _assigned(tree: ast.Module, name: str) -> object:
    """The literal a module-level `name = …` (or `name: T = …`) assigns."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if name in targets and node.value is not None:
            return ast.literal_eval(node.value)
    raise KeyError(name)


def _parents(down: object) -> tuple[str, ...]:
    if down is None:
        return ()
    if isinstance(down, str):
        return (down,)
    if isinstance(down, (tuple, list)):
        return tuple(down)
    raise TypeError(f"unexpected down_revision {down!r}")


def load_graph(files: list[Path]) -> Graph:
    """Revision → its parents. Raises on a revision defined twice."""
    graph: Graph = {}
    for path in files:
        tree = ast.parse(path.read_text())
        revision = _assigned(tree, "revision")
        if not isinstance(revision, str):
            raise TypeError(f"{path.name}: revision is not a string")
        if revision in graph:
            raise AssertionError(f"revision {revision} is defined twice ({path.name})")
        graph[revision] = _parents(_assigned(tree, "down_revision"))
    return graph


def problems(graph: Graph, release_heads: dict[str, str]) -> list[str]:
    """Everything wrong with the graph, as sentences. Empty means it holds."""
    found: list[str] = []

    for rev, parents in sorted(graph.items()):
        if len(parents) > 1:
            found.append(f"{rev} is a merge revision (parents {', '.join(parents)})")
        for parent in parents:
            if parent not in graph:
                found.append(f"{rev} names a parent that does not exist: {parent}")

    roots = sorted(rev for rev, parents in graph.items() if not parents)
    if len(roots) != 1:
        found.append(f"expected one root, found {len(roots)}: {roots}")

    children = {parent for parents in graph.values() for parent in parents}
    heads = sorted(rev for rev in graph if rev not in children)
    if len(heads) != 1:
        found.append(f"expected one head, found {len(heads)}: {heads}")
        return found

    # Walk from the head to the root. The graph is linear if we get here
    # without a merge, so the walk visits every ancestor exactly once.
    ancestry: set[str] = set()
    rev: str | None = heads[0]
    while rev is not None and rev not in ancestry:
        ancestry.add(rev)
        parents = graph.get(rev, ())
        rev = parents[0] if parents else None

    for line, release_head in sorted(release_heads.items()):
        if release_head not in graph:
            found.append(f"the {line} release head {release_head} is not in this branch at all")
        elif release_head not in ancestry:
            found.append(
                f"the {line} release head {release_head} is not an ancestor of the head "
                f"{heads[0]}: a {line} deployment would not upgrade cleanly"
            )
    return found


def _release_heads() -> dict[str, str]:
    heads: dict[str, str] = json.loads(_RELEASE_HEADS.read_text())["release_heads"]
    return heads


# ── the real graph ───────────────────────────────────────────────────────────


def test_the_graph_is_one_line_with_every_release_head_on_it() -> None:
    assert problems(load_graph(_migration_files()), _release_heads()) == []


def test_the_release_heads_ledger_is_populated() -> None:
    """A guard with nothing to check passes vacuously."""
    heads = _release_heads()
    assert heads.get("1.6") == "8c02c0a6b39b"


# ── the checks themselves, against synthetic graphs ──────────────────────────


LINE: Graph = {"a": (), "b": ("a",), "c": ("b",)}


def test_a_straight_line_passes() -> None:
    assert problems(LINE, {"1.0": "b"}) == []


def test_the_head_itself_counts_as_on_the_line() -> None:
    assert problems(LINE, {"1.0": "c"}) == []


def test_two_heads_fail() -> None:
    out = problems({**LINE, "d": ("b",)}, {})
    assert any("expected one head" in p for p in out)


def test_a_merge_revision_fails() -> None:
    out = problems({**LINE, "d": ("b",), "e": ("c", "d")}, {})
    assert any("merge revision" in p for p in out)


def test_a_dangling_parent_fails() -> None:
    out = problems({**LINE, "d": ("zzz",)}, {})
    assert any("does not exist: zzz" in p for p in out)


def test_two_roots_fail() -> None:
    out = problems({**LINE, "x": ()}, {})
    assert any("expected one root" in p for p in out)


def test_a_release_head_off_the_line_fails() -> None:
    """The 1.6 case: `main`'s first migration re-parented back onto the
    branch point would leave 1.6's head on a side branch."""
    branched: Graph = {"a": (), "rel": ("a",), "m1": ("a",), "m2": ("m1",)}
    out = problems(branched, {"1.6": "rel"})
    assert any("expected one head" in p for p in out)


def test_a_release_head_missing_from_the_branch_fails() -> None:
    """A release line's migrations were never carried forward."""
    out = problems(LINE, {"1.7": "not-here"})
    assert any("not in this branch at all" in p for p in out)


def test_a_release_line_carried_forward_passes() -> None:
    """Re-parenting done right: the release migration sits in the chain, not
    beside it."""
    carried: Graph = {"a": (), "rel": ("a",), "m1": ("rel",), "m2": ("m1",)}
    assert problems(carried, {"1.6": "rel"}) == []


def test_a_revision_defined_twice_is_refused(tmp_path: Path) -> None:
    for name in ("one", "two"):
        (tmp_path / f"{name}.py").write_text('revision = "dup"\ndown_revision = None\n')
    with pytest.raises(AssertionError, match="defined twice"):
        load_graph(sorted(tmp_path.glob("*.py")))


def test_annotated_assignments_are_read(tmp_path: Path) -> None:
    """Alembic's template writes `down_revision: Union[str, None] = …`."""
    (tmp_path / "a.py").write_text('revision: str = "a"\ndown_revision: str | None = None\n')
    (tmp_path / "b.py").write_text('revision: str = "b"\ndown_revision: str | None = "a"\n')
    assert load_graph(sorted(tmp_path.glob("*.py"))) == {"a": (), "b": ("a",)}
