"""A configure serialises against the workspace's apply-capable runs (#1975).

Three properties, and each is here because a plausible change breaks it
silently rather than loudly:

* the in-flight status set has ONE producer, and `configuring` is in it;
* every module that creates a configure handles the lock refusal, so it
  never arrives as a 500 or as an error logged once per scheduler cycle;
* a plan is NOT refused at create, which is the half a careless fix breaks.

The set is checked per CALL SITE rather than by greppingfor the constant,
because the defect this replaced was two hand-written copies that agreed only
by a docstring saying they did.
"""

import ast
import pathlib

import pytest

from terrapod.services import run_service, workspace_settings

_RUN_SERVICE = pathlib.Path(run_service.__file__)

#: The two functions that must agree about "this workspace is busy".
_SLOT_READERS = ("_apply_slot_holder", "claim_next_run")

#: What the old hand-written literal looked like. If this reappears anywhere,
#: a third copy has been introduced and the one-producer property is gone.
_OLD_LITERAL = '"planning", "planned", "confirmed", "applying", "canceling"'


class TestTheMutatingSlotSet:
    def test_configuring_holds_the_slot(self):
        """The whole point of #1975: a configure mutates real hosts, so an apply
        must not start underneath one."""
        assert "configuring" in run_service.MUTATING_SLOT_STATES

    def test_the_membership_is_exactly_this(self):
        """Exact rather than a subset check.

        A floor alone catches the set EMPTYING and not the set SHRINKING, and
        shrinking is the realistic accident -- dropping `planned`, say, would
        let a run awaiting confirmation stop holding the workspace.
        """
        assert set(run_service.MUTATING_SLOT_STATES) == {
            "planning",
            "planned",
            "confirmed",
            "applying",
            "configuring",
            "canceling",
        }

    def test_terminal_states_hold_nothing(self):
        assert not (set(run_service.MUTATING_SLOT_STATES) & set(run_service.TERMINAL_STATES))

    @pytest.mark.parametrize("func", _SLOT_READERS)
    def test_each_reader_uses_the_shared_set(self, func):
        """Per-site, so removing the constant from ONE of the two fails.

        A module-wide grep for the name passes while one reader has gone back
        to a literal, which is exactly how the two drifted before.
        """
        tree = ast.parse(_RUN_SERVICE.read_text())
        target = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
        )
        names = {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        assert "MUTATING_SLOT_STATES" in names, (
            f"{func}() no longer reads MUTATING_SLOT_STATES. If it has its own copy of "
            "the status set, the two readers can disagree again -- which is the defect "
            "this constant exists to remove."
        )

    def test_no_hand_written_copy_survives(self):
        assert _OLD_LITERAL not in _RUN_SERVICE.read_text(), (
            "a hand-written copy of the in-flight status set is back; it must read "
            "MUTATING_SLOT_STATES instead."
        )


def _configure_creators() -> dict[str, pathlib.Path]:
    """Modules that ASK for a configure, derived from the tree.

    Derived rather than listed: a new configure creator must inherit the
    obligation automatically, which a hand-kept list cannot deliver.

    Matches any call passing `is_configure=True`, NOT specifically
    `create_run(...)`. The narrow form was tried first and was blind to
    `vcs_poller`, which asks through its own `_create_vcs_run` helper -- the
    same class of blind spot as a matcher that reads only the keyword form of
    an argument. "Asks for a configure" is the property that carries the
    obligation; which function it reaches `create_run` through is incidental.
    """
    root = pathlib.Path(run_service.__file__).parent.parent
    found: dict[str, pathlib.Path] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "is_configure"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    found[path.name] = path
    return found


class TestTheLockRefusalReachesEveryConfigureCreator:
    def test_the_creator_set_is_not_empty(self):
        """A floor. Without it a renamed helper makes every assertion below pass
        by inspecting nothing."""
        creators = _configure_creators()
        assert len(creators) >= 3, (
            f"only found {len(creators)} configure creators ({sorted(creators)}); the "
            "matcher has probably stopped matching rather than the callers having gone"
        )

    def test_every_creator_handles_the_refusal(self):
        """`WorkspaceLocked` is raised at the chokepoint, so a creator that does
        not handle it either 500s a request or logs an error every cycle about a
        workspace behaving exactly as its operator asked."""
        missing = [
            name
            for name, path in _configure_creators().items()
            if "WorkspaceLocked" not in path.read_text()
        ]
        assert missing == [], (
            f"module(s) {missing} create a configure but do not mention WorkspaceLocked. "
            "A platform-decided creator catches and skips; a requested one is translated "
            "by its router."
        )


class TestTheLockRefusalItself:
    def test_a_configure_on_a_locked_workspace_is_refused(self):
        ws = type("W", (), {"locked": True})()
        with pytest.raises(workspace_settings.WorkspaceLocked):
            workspace_settings.refuse_configure_on_locked_workspace(ws, is_configure=True)

    def test_a_plan_on_a_locked_workspace_is_not(self):
        """The contrast, and the half a careless fix breaks.

        A plan or apply is held at DISPATCH and queued until the lock comes off.
        Refusing it here would change behaviour for every Terraform workspace.
        """
        ws = type("W", (), {"locked": True})()
        workspace_settings.refuse_configure_on_locked_workspace(ws, is_configure=False)

    def test_a_configure_on_an_unlocked_workspace_is_not(self):
        ws = type("W", (), {"locked": False})()
        workspace_settings.refuse_configure_on_locked_workspace(ws, is_configure=True)

    def test_it_is_a_value_error(self):
        """So a caller with a bare `except ValueError` still catches it, which is
        the stated reason its two siblings are ValueErrors too."""
        assert issubclass(workspace_settings.WorkspaceLocked, ValueError)
