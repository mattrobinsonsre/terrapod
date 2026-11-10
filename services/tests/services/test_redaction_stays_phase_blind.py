"""Prompt redaction must resolve variables PHASE-BLIND (#2189).

`summariser._sensitive_literals` builds the list of literals scrubbed from the
AI prompt, so it wants every sensitive value that could appear -- including an
apply-only one, which must still be redacted if it ever reaches a log.

Passing a phase there would narrow the list and make redaction WEAKER, and
nothing behavioural would notice: the prompt would simply contain a secret it
used to mask. The obvious tidy-up is to "pass the phase like the delivery site
does", so the asymmetry needs a gate rather than a comment.
"""

import ast
import pathlib

from terrapod.services import summariser


def _resolve_calls(func_name: str, module) -> list[ast.Call]:
    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    target = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == func_name
    )
    return [
        n
        for n in ast.walk(target)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "resolve_variables"
    ]


class TestRedactionIsPhaseBlind:
    def test_the_summariser_resolves_without_a_phase(self):
        calls = _resolve_calls("_sensitive_literals", summariser)
        assert calls, "no resolve_variables call found -- has the helper been renamed?"
        for call in calls:
            passed = {kw.arg for kw in call.keywords}
            assert "phase" not in passed, (
                "redaction must stay phase-blind: narrowing it drops apply-only "
                "secrets from the literals scrubbed from the model prompt"
            )
            # positional too, since `resolve_variables(db, ws, "apply")` would
            # bypass a keyword-only check
            assert len(call.args) <= 2, (
                f"a third positional argument narrows the resolution: {len(call.args)} given"
            )

    def test_the_delivery_site_DOES_pass_a_phase(self):
        """The other half of the asymmetry. If this ever stops holding, every
        apply-only variable is silently delivered to plans as well -- the
        feature present and inert, which is the defect class this repo keeps
        meeting."""
        import ast

        from terrapod.api.routers import runs

        # Read as a CALL, not as a line of text. This asserted the literal
        # single-line spelling and broke the moment the call gained a second
        # keyword and ruff wrapped it (#2377) -- a true property failing on its
        # own formatting. The keyword is what it means; the layout is not.
        tree = ast.parse(pathlib.Path(runs.__file__).read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_variables"
        ]
        assert calls, "no resolve_variables call found in the run-delivery router"
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords}
            assert "phase" in kwargs, (
                "the run-delivery site must narrow by phase; this call does not: "
                f"line {call.lineno}"
            )


class TestTheResourceListResolverIsPhaseBlindToo:
    """`plan_resource_list.strict_sensitive_literals` builds the list of values
    scrubbed from the PR comment, so it has the same requirement as the
    summariser's: every sensitive value that could appear, apply-only included.
    Narrowing it would make the redaction weaker with nothing behavioural to
    notice, which is the reason the summariser's has this gate."""

    def test_it_resolves_without_a_phase(self):
        from terrapod.services import plan_resource_list

        calls = _resolve_calls("strict_sensitive_literals", plan_resource_list)
        assert calls == [] or all(
            "phase" not in {kw.arg for kw in c.keywords} and len(c.args) <= 2 for c in calls
        )

    def test_it_makes_a_resolve_call_at_all(self):
        """Attribute calls (`variable_service.resolve_variables(...)`) are the
        shape used here, so look for either spelling rather than passing
        vacuously because the Name-only walk above finds nothing."""
        import ast
        import pathlib

        from terrapod.services import plan_resource_list

        tree = ast.parse(pathlib.Path(plan_resource_list.__file__).read_text())
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "strict_sensitive_literals"
        )
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == "resolve_variables")
                or (isinstance(n.func, ast.Name) and n.func.id == "resolve_variables")
            )
        ]
        assert calls, "no resolve_variables call found -- has the helper been renamed?"
        for call in calls:
            assert "phase" not in {kw.arg for kw in call.keywords}
            assert len(call.args) <= 2, "a third positional argument narrows the resolution"
