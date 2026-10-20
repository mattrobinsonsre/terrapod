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
        from terrapod.api.routers import runs

        src = pathlib.Path(runs.__file__).read_text()
        assert "resolve_variables(db, run.workspace_id, phase=phase)" in src, (
            "the run-delivery site must narrow by phase"
        )
