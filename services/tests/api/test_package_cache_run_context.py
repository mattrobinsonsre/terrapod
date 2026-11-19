"""The run context reaches `get_or_fetch`, and nothing else (#2589).

A refusal is notified to the workspace that asked for the artifact, which means
every package-cache route has to hand `get_or_fetch` the run on whose behalf it
is fetching. That is six call sites, and threading an argument through six
places is exactly the edit that goes wrong quietly.

**It went wrong quietly here.** Two of the six were rewritten with a regex whose
character class could not cross a closing parenthesis, so for a call shaped

    get_or_fetch(db, storage, npm.artifact_for(name, version, entry))

the keyword landed on the INNER call -- `npm.artifact_for(..., run_id=...)` --
which takes no such parameter. Every npm and every Go-module fetch raised
`TypeError`. No unit test saw it, because none of them drives those two routes
through the real `artifact_for`; the air-gap gate caught it, seventeen minutes
into a twenty-minute job, as a hang rather than an error.

So this gate reads the ROUTER'S OWN SOURCE rather than any call's behaviour: a
misplaced keyword is a syntactically perfect call that fails only when executed,
and the failure surfaces far from the edit.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from terrapod.api.routers import package_cache

_SOURCE = pathlib.Path(package_cache.__file__)
_TREE = ast.parse(_SOURCE.read_text())


def _calls() -> list[ast.Call]:
    return [n for n in ast.walk(_TREE) if isinstance(n, ast.Call)]


def _name_of(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    return getattr(call.func, "attr", "")


class TestTheRunContextIsPassedToTheRightCall:
    def test_every_get_or_fetch_carries_the_run(self) -> None:
        """A route that omits it cannot notify anyone when the artifact is
        refused -- the refusal still happens, but silently."""
        missing = [
            c.lineno
            for c in _calls()
            if _name_of(c) == "get_or_fetch" and "run_id" not in {k.arg for k in c.keywords}
        ]
        assert not missing, (
            f"get_or_fetch without run_id at line(s) {missing}. A package-cache route "
            "fetches on behalf of a run, and a refusal there has to reach that run's "
            "workspace."
        )

    def test_the_floor_binds(self) -> None:
        """Without this, a matcher that stops matching passes on nothing."""
        found = [c for c in _calls() if _name_of(c) == "get_or_fetch"]
        assert len(found) >= 6, (
            f"only {len(found)} get_or_fetch call(s) found; the matcher has drifted"
        )

    @pytest.mark.parametrize("builder", ["artifact_for", "document_artifact"])
    def test_no_artifact_builder_is_handed_the_run(self, builder: str) -> None:
        """The defect this file exists for. These construct an `Artifact`; they
        take no run and never should -- an artifact is the same artifact whoever
        asked for it. A `run_id` here is a keyword that slid one call inwards."""
        wrong = [
            c.lineno
            for c in _calls()
            if _name_of(c) == builder and "run_id" in {k.arg for k in c.keywords}
        ]
        assert not wrong, (
            f"{builder}() was handed run_id at line(s) {wrong} -- it takes no such "
            "parameter, so this raises TypeError on every fetch through that route. "
            "The keyword belongs on the enclosing get_or_fetch(...)."
        )

    def test_the_builders_are_actually_present(self) -> None:
        """So the test above cannot pass by matching nothing."""
        builders = [c for c in _calls() if _name_of(c) in ("artifact_for", "document_artifact")]
        assert len(builders) >= 4, f"only {len(builders)} artifact builder call(s) found"
