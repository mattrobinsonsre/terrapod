"""Everything the LISTENER imports must actually be in the listener image.

The image is deliberately minimal — it COPYs named files plus `engines/` and
`runner/`, not the whole tree — so an import added along the listener's path that
reaches a package the image does not ship passes every test and then fails at
runtime with `No module named 'terrapod.X'`. Not hypothetical: adding request
signing imported `terrapod.auth.listener_pop`, which was absent, and both Eval Boot
legs failed while the entire python suite was green.

The walk starts at the listener's entrypoint and follows only what it actually
imports. That matters: `runner/phases/` ships in the image but belongs to the
runner Job, is never imported by the listener, and legitimately reaches
`terrapod.services` and `terrapod.gpg_verify` — packages the RUNNER image copies.
Checking all of `runner/` against the listener's COPY list reports those three as
missing, which is the wrong answer.
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "terrapod"


def _dockerfile() -> pathlib.Path:
    """Find Dockerfile.listener under either layout.

    The repository has it at `<repo>/docker/` with the python under
    `<repo>/services/`, so one level UP from this test's root. The test image
    flattens `services/` into `/app` and copies `docker/` beside it, so there it is
    one level DOWN. A path hard-coded for either resolves to nothing in the other —
    which is how this test passed locally and then failed the unit shard on a
    FileNotFoundError, the same shape of mistake as the missing COPY it exists to
    catch.
    """
    for cand in (ROOT.parent / "docker", ROOT / "docker"):
        f = cand / "Dockerfile.listener"
        if f.is_file():
            return f
    raise AssertionError(
        "Dockerfile.listener not found beside or above the test root — if the test "
        "image stopped copying docker/, this guard is silently not running."
    )


DOCKERFILE = _dockerfile()

#: Where the listener process starts.
ENTRYPOINTS = ("runner/__main__.py", "runner/listener.py")


def _shipped() -> set[str]:
    out: set[str] = set()
    for line in DOCKERFILE.read_text().splitlines():
        m = re.match(r"COPY\s+services/terrapod/([A-Za-z0-9_]+)(\.py|/)", line.strip())
        if m:
            out.add(m.group(1))
    return out


def _module_imports(path: pathlib.Path) -> set[str]:
    """`terrapod.*` modules this file imports AT IMPORT TIME.

    Imports nested inside a function or class are excluded, and that exclusion is
    the point rather than a convenience: a function-local import only fails if the
    function runs, which is how a module shared between the API and the listener can
    reach a server-only package safely. `listener_pop` does exactly this — the
    replay check imports `terrapod.redis.client` inside `_claim_nonce`, which only
    the server's verify path calls, so the listener imports the module and signs
    with it while never touching redis. Treating that as a missing dependency would
    report a failure that cannot happen and push someone to ship redis into the
    listener image for no reason.

    Imports inside a top-level `try:` or `if` still execute on import, so they count.
    """
    mods: set[str] = set()
    tree = ast.parse(path.read_text())

    def visit(node: ast.AST, *, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            nested = lazy or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            if not nested:
                if (
                    isinstance(child, ast.ImportFrom)
                    and child.module
                    and child.module.startswith("terrapod.")
                ):
                    mods.add(child.module)
                elif isinstance(child, ast.Import):
                    for a in child.names:
                        if a.name.startswith("terrapod."):
                            mods.add(a.name)
            visit(child, lazy=nested)

    visit(tree, lazy=False)
    return mods


def _reachable() -> dict[str, set[str]]:
    """Transitive closure from the entrypoints, as {file: terrapod modules}."""
    seen: set[str] = set()
    out: dict[str, set[str]] = {}
    queue = [SRC / e for e in ENTRYPOINTS if (SRC / e).is_file()]
    assert queue, f"no entrypoint found among {ENTRYPOINTS}"
    while queue:
        f = queue.pop()
        rel = str(f.relative_to(SRC))
        if rel in seen:
            continue
        seen.add(rel)
        mods = _module_imports(f)
        out[rel] = mods
        for m in mods:
            cand = SRC / (m.removeprefix("terrapod.").replace(".", "/") + ".py")
            if cand.is_file():
                queue.append(cand)
    return out


class TestTheListenerImageShipsWhatTheListenerImports:
    def test_every_package_on_the_listeners_import_path_is_copied(self):
        shipped = _shipped()
        assert shipped, "could not parse any COPY lines — has the Dockerfile moved?"
        missing: list[str] = []
        for rel, mods in _reachable().items():
            for m in sorted(mods):
                top = m.split(".")[1]
                if top not in shipped:
                    missing.append(f"{rel} imports {m}")
        assert not missing, (
            "the listener's import path reaches terrapod packages its image does not "
            "COPY, so it fails at runtime with ModuleNotFoundError while every test "
            "passes:\n  "
            + "\n  ".join(missing)
            + f"\n\nShipped: {sorted(shipped)}\nAdd a COPY line to docker/Dockerfile.listener."
        )
