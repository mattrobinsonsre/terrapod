"""The configure ceiling is sent for a configure and for nothing else (#1977).

#1977 carries the standing requirement that a deployment running only
terraform/tofu "must pay nothing" for Ansible work. For this change that is a
POSITIONAL property of one line: `timeout-minutes` is set inside `next_run`'s
`phase == "configure"` branch, so every plan, apply, preview and up keeps the
Job builder's own 60-minute default and the spec the listener has always built.

A behavioural test cannot see this -- it would have to drive `next_run` for
every phase and assert an absence -- and the wire-contract snapshot cannot
either, because it records that the key exists, not which branch sets it.
"""

from __future__ import annotations

import ast
import inspect

from terrapod.api.routers import runs as runs_router


def _next_run_source() -> str:
    whole = inspect.getsource(runs_router)
    tree = ast.parse(whole)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "next_run":
            return ast.get_source_segment(whole, node) or ""
    raise AssertionError("next_run not found in the runs router")


class TestTheCeilingIsConfigureOnly:
    def test_it_is_set_exactly_once(self):
        src = _next_run_source()
        assert src.count('"timeout-minutes"') == 1, (
            "set in more than one place, so one of them is reachable by a phase "
            "that should keep the builder default"
        )

    def test_it_is_set_INSIDE_the_configure_branch(self):
        """The whole of 'terraform pays nothing' is this line's position."""
        src = _next_run_source()
        guard = src.index('if phase == "configure"')
        assign = src.index('"timeout-minutes"')
        assert assign > guard, "set before the configure guard, so every phase gets it"

        # ...and inside it, not merely after: the guard's block is the run of
        # lines indented more deeply than the `if` itself.
        lines = src[guard:].splitlines()
        base = len(lines[0]) - len(lines[0].lstrip())
        end = next(
            (
                i
                for i, ln in enumerate(lines[1:], start=1)
                if ln.strip() and (len(ln) - len(ln.lstrip())) <= base
            ),
            len(lines),
        )
        assert any("timeout-minutes" in ln for ln in lines[1:end]), (
            "after the configure branch rather than within it"
        )


class TestTheListenerForwardsItWithABackwardCompatibleDefault:
    """The call site, which is where this kind of wiring actually fails.

    An API that predates this sends nothing, and the listener must then build
    exactly the spec it always did -- so the fallback has to be the builder's
    own 60, not 0 (which would make `activeDeadlineSeconds` 0) and not absent.
    """

    def test_the_listener_passes_it_from_the_attribute(self):
        """Asserted STRUCTURALLY rather than as a literal.

        This read was hoisted into a local when the runner token had to be
        sized from the same value (#2526) -- one read, two uses -- and the
        literal this test used to match (`timeout_minutes=int(attrs.get(...)
        or 60)` as one call-site expression) stopped existing. A test pinned
        to an exact expression fails on a refactor that STRENGTHENS the
        property it guards, which is the worst way for a guard to behave.

        So the property, not the spelling: whatever the Job build is handed
        must be read from the attribute, and must fall back to the builder's
        own default.
        """
        from terrapod.runner import listener

        tree = ast.parse(inspect.getsource(listener))

        build = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.Call) and any(kw.arg == "timeout_minutes" for kw in n.keywords)
            ),
            None,
        )
        assert build is not None, "nothing in the listener passes timeout_minutes at all"

        passed = next(kw.value for kw in build.keywords if kw.arg == "timeout_minutes")
        assert isinstance(passed, ast.Name), (
            "the Job build is handed an inline expression for timeout_minutes. "
            "That is not wrong in itself, but it means the runner token can no "
            "longer be sized from the same value (#2526) -- see "
            "tests/runner/test_listener_token_ttl.py, which requires one read."
        )

        produced = {
            ast.unparse(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == passed.id for t in n.targets)
        }
        assert produced, f"nothing assigns {passed.id}, so its origin cannot be checked"
        # Matched WITHOUT the quotes: ast.unparse normalises string literals to
        # single quotes, so a matcher written with the source's double quotes
        # finds nothing and fails on correct code.
        assert any("attrs.get(" in e and "timeout-minutes" in e for e in produced), (
            f"{passed.id} is not read from the timeout-minutes attribute, so the API "
            f"can send the ceiling and the Job will still carry the builder default: "
            f"{sorted(produced)}"
        )
        assert any("60" in e for e in produced), (
            "the fallback is no longer the builder's own 60, so an API that predates "
            f"this would make the listener build a different spec: {sorted(produced)}"
        )

    def test_the_builder_default_is_what_the_fallback_names(self):
        """If the builder's default moves, the listener's fallback becomes a
        second copy that silently disagrees with it."""
        from terrapod.runner.job_template import build_job_spec

        default = inspect.signature(build_job_spec).parameters["timeout_minutes"].default
        assert default == 60, (
            f"builder default is now {default}; the listener's fallback still says 60"
        )
