"""The workspace's plan/apply declaration is enforced on the run path (#2116).

#1966 gave a workspace `plan_apply_enabled` / `configure_enabled`. The
declaration was stored, serialised, bulk-settable, templated by autodiscovery
and restored with a deleted workspace -- and **nothing on the run path read
either column**. A workspace declaring `plan_apply_enabled=False` still
accepted and dispatched a plan and an apply, from all fifteen run creators.

That is the flag-nothing-reads defect, on a column an operator had deliberately
set. `test_capabilities.TestEveryFlagIsRead` exists to catch exactly this
class and could not see it, because it is derived from the `CAPABILITIES`
table and these are two ordinary workspace columns.

So there are two gates here, and they guard different halves:

  1. **The rule is enforced at the chokepoint, before any work.**
     `run_service.create_run` is the only `Run` constructor on the live path,
     so one call there is what fifteen callers cannot route around. The gate is
     positional as well as by-name: a check placed *after* the `Run` is built
     would let the row exist, and a check that moved below the version
     resolution would waste the work it exists to avoid.

  2. **Every call site decides how to react, and none re-states the rule.**
     Derived from the source with no exemption ledger: a sixteenth caller fails
     this until it chooses. What a caller may choose is only 4xx or skip -- the
     refusal itself is `create_run`'s, so the two cannot drift.

The second gate is deliberately NOT "does the file mention the exception". A
presence check is not an application check, which is the lesson this file's
twin (`test_operations_pair_enforcement`) was written for: it asks whether the
handler is attached to the statement that makes the call.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "terrapod"
_EXC = "PlanApplyNotEnabled"
_RULE = "require_plan_apply_operation"

#: A floor, so a rename cannot make this gate pass by matching nothing.
#: Fifteen callers today; the floor leaves room for a few to be retired
#: without failing here, while still catching a matcher that finds none.
MIN_CALL_SITES = 10


def _py_files() -> list[pathlib.Path]:
    return sorted(_SRC.rglob("*.py"))


def _call_sites() -> list[tuple[pathlib.Path, ast.AST, ast.stmt | None]]:
    """Every `create_run(...)` call under `terrapod/`, with the statement it
    sits in -- excluding the definition itself and the dev seeder."""
    out: list[tuple[pathlib.Path, ast.AST, ast.stmt | None]] = []
    for f in _py_files():
        if f.name == "bootstrap.py":  # the dev seeder builds a Run directly
            continue
        tree = ast.parse(f.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name == "create_run" and f.name == "run_service.py":
                continue  # the definition
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                name = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                if name != "create_run":
                    continue
                out.append((f, fn, sub))
    return out


def _handlers_guarding(fn: ast.AST, call: ast.Call) -> list[ast.ExceptHandler]:
    """The `except` handlers whose `try` body encloses `call`."""
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        lo = min(s.lineno for s in node.body)
        hi = max((s.end_lineno or s.lineno) for s in node.body)
        if lo <= call.lineno <= hi:
            found.extend(node.handlers)
    return found


class TestTheChokepointEnforces:
    def test_create_run_calls_the_rule(self):
        src = (_SRC / "services" / "run_service.py").read_text()
        tree = ast.parse(src)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "create_run"
        )
        calls = [
            c
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
            and (getattr(c.func, "attr", None) or getattr(c.func, "id", None)) == _RULE
        ]
        assert calls, (
            f"`create_run` no longer calls `{_RULE}`. That call is the ONLY thing "
            "enforcing the workspace's plan/apply declaration — without it, "
            "`plan-apply-enabled: false` is accepted by every write surface and "
            "ignored by all fifteen run creators (#2116)."
        )
        # ...and it is handed the workspace, not something derived from it.
        arg = calls[0].args[0] if calls[0].args else None
        assert isinstance(arg, ast.Name) and arg.id == "workspace", (
            f"`{_RULE}` must be passed `workspace` directly; got "
            f"{ast.dump(arg) if arg else 'no positional argument'}."
        )

    def test_it_is_enforced_before_the_run_is_built(self):
        """A check after the `Run(...)` would let the row exist first."""
        src = (_SRC / "services" / "run_service.py").read_text()
        tree = ast.parse(src)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "create_run"
        )
        rule_line = min(
            c.lineno
            for c in ast.walk(fn)
            if isinstance(c, ast.Call)
            and (getattr(c.func, "attr", None) or getattr(c.func, "id", None)) == _RULE
        )
        run_line = min(
            c.lineno
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "Run"
        )
        assert rule_line < run_line, (
            f"`{_RULE}` is called at line {rule_line}, after the `Run(...)` at "
            f"{run_line}. The refusal has to come first, or the row is built for a "
            "run that cannot exist."
        )


class TestEveryCallSiteDecides:
    def test_at_least_the_known_call_sites_are_found(self):
        sites = _call_sites()
        assert len(sites) >= MIN_CALL_SITES, (
            f"found only {len(sites)} `create_run` call sites, expected at least "
            f"{MIN_CALL_SITES}. If it was renamed, this gate is now matching "
            "nothing and proving nothing."
        )

    def test_each_one_handles_the_refusal(self):
        offenders = []
        for f, fn, call in _call_sites():
            handlers = _handlers_guarding(fn, call)
            named = any(_EXC in ast.dump(h.type) if h.type is not None else False for h in handlers)
            if not named:
                offenders.append(f"{f.relative_to(_SRC)}:{call.lineno} in {fn.name}()")
        assert not offenders, (
            "these `create_run` call sites do not handle "
            f"`{_EXC}`:\n  " + "\n  ".join(offenders) + "\n\n"
            "Every caller has to decide what the refusal means for it (#2116): a run "
            "somebody ASKED for translates it into a 4xx naming the column; a run the "
            "platform decided on its own catches it and skips, because erroring once "
            "per poll cycle is noise about a workspace behaving exactly as declared. "
            "What a caller must NOT do is re-state the rule — the refusal belongs to "
            "`create_run` alone so the two cannot drift."
        )

    def test_no_call_site_re_states_the_rule(self):
        """A caller testing the column itself is a second copy of the rule."""
        offenders = []
        for f, fn, _call in _call_sites():
            if f.name == "run_service.py":
                continue
            body = ast.dump(fn)
            if "plan_apply_enabled" in body:
                offenders.append(f"{f.relative_to(_SRC)}:{fn.lineno} in {fn.name}()")
        assert not offenders, (
            "these run creators read `plan_apply_enabled` themselves:\n  "
            + "\n  ".join(offenders)
            + f"\n\nThe rule lives in `{_RULE}` and is enforced in `create_run`. A "
            "caller that checks the column too is a second copy that will drift — "
            "catch the refusal instead."
        )


@pytest.mark.parametrize("name", [_EXC, _RULE])
def test_the_rule_and_its_exception_still_exist(name):
    src = (_SRC / "services" / "workspace_settings.py").read_text()
    assert name in src, (
        f"`{name}` is gone from workspace_settings. It is the enforcement twin of "
        "`validate_operations` and both gates above depend on it (#2116)."
    )
