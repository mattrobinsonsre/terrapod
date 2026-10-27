"""Apply-only variables: the resolution, and the inversion that must not ship (#2189).

A plan executes the configuration's code, so a credential only the apply needs
must not be in the plan's environment. The flag withholds it.

The fixtures seed BOTH rows of a duplicate pair deliberately. A fixture holding
only one row would pass whether or not the filter is applied -- the weak shape
that has shipped here before -- so every test below can actually fail.
"""

import pytest

from terrapod.services import variable_service


class _Row:
    """The attributes `resolve_variables` reads off a variable row."""

    def __init__(
        self,
        key,
        value,
        apply_only=False,
        category="env",
        sensitive=False,
        configure_only=False,
    ):
        self.key = key
        self.value = value
        self.apply_only = apply_only
        self.configure_only = configure_only
        self.category = category
        self.structured = False
        self.sensitive = sensitive
        self.value_source = "static"


class _Set:
    def __init__(self, *rows):
        self.variables = list(rows)


@pytest.fixture
def seed(monkeypatch):
    """Install the three precedence tiers, each with whatever rows a test wants."""

    def _seed(workspace=(), priority=(), non_priority=()):
        async def _varsets(db, wid, priority_flag=False, **kw):
            rows = priority if priority_flag else non_priority
            return [_Set(*rows)] if rows else []

        async def _varsets_kw(db, wid, priority=False):
            return await _varsets(db, wid, priority)

        async def _list(db, wid):
            return list(workspace)

        monkeypatch.setattr(variable_service, "_get_applicable_varsets", _varsets_kw)
        monkeypatch.setattr(variable_service, "list_variables", _list)

    return _seed


async def _resolve(phase):
    out = await variable_service.resolve_variables(None, None, phase=phase)
    return sorted((v.key, v.value) for v in out)


class TestTheDuplicatePair:
    """One standard row and one apply-only row, same key -- the whole feature."""

    PAIR = (_Row("TOKEN", "read-only"), _Row("TOKEN", "write", apply_only=True))

    async def test_a_plan_gets_the_standard_value_and_not_the_apply_only_one(self, seed):
        seed(workspace=self.PAIR)
        # Asserting the standard value is PRESENT, not merely that the apply-only
        # one is absent: an empty result would satisfy the weaker assertion.
        assert await _resolve("plan") == [("TOKEN", "read-only")]

    async def test_an_apply_gets_the_apply_only_value(self, seed):
        seed(workspace=self.PAIR)
        assert await _resolve("apply") == [("TOKEN", "write")]

    async def test_a_CONFIGURE_gets_the_apply_only_value(self, seed):
        """A configure is on the apply side of this line (#1933).

        It ran on the plan side until `PHASES_SEEING_APPLY_ONLY` existed, so a
        configure silently dropped every apply-only variable: #2189 added the
        filter when `apply` and `plan` were the only phases, #2194 added
        `configure`, and it fell into "any other phase". Each shipped correct
        and they were wrong together.

        A configure runs the repository's playbooks against real hosts, so it is
        a write with apply-equivalent blast radius (#1976), not a speculative
        read of a pull request's code. Asserting the apply-only VALUE is
        present, not merely that something came back -- the standard value
        would satisfy the weaker assertion.
        """
        seed(workspace=self.PAIR)
        assert await _resolve("configure") == [("TOKEN", "write")]

    async def test_the_phases_seeing_apply_only_are_exactly_apply_and_configure(self):
        """Pinned as a set, so adding a phase is a decision rather than a
        default. A new phase that silently inherits either answer is how this
        went wrong the first time."""
        assert variable_service.PHASES_SEEING_APPLY_ONLY == {"apply", "configure"}

    async def test_the_phases_seeing_configure_only_are_exactly_configure(self):
        """Pinned as a set for the same reason (#2190). Widening this to include
        `apply` would make the flag identical to `apply_only` and silently
        deliver the fleet's credential to every apply."""
        assert variable_service.PHASES_SEEING_CONFIGURE_ONLY == {"configure"}

    async def test_every_withholding_phase_is_one_a_CLAIM_CAN_YIELD(self):
        """The reader half, derived from `claim_next_run`'s own tuple list.

        A withholding flag is INERT unless some claim hands the delivery site
        that phase: `runs.py` resolves with the phase `claim_next_run` returned,
        so a phase set naming something no claim yields would withhold a
        variable from every run and deliver it to none -- a flag nothing reads,
        which this repository has shipped four times under other names.

        Derived by parsing the claim loop rather than by listing the phases
        here, because a hand-kept list would be a second place for the answer to
        live and the drift would be invisible: both sides would still look
        complete.
        """
        import ast
        import pathlib as _pathlib

        from terrapod.services import run_service

        tree = ast.parse(_pathlib.Path(run_service.__file__).read_text())
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "claim_next_run"
        )
        # The loop's iterable is a list of (target_status, phase, next_status).
        yielded = {
            elt.elts[1].value
            for node in ast.walk(fn)
            if isinstance(node, ast.For) and isinstance(node.iter, ast.List)
            for elt in node.iter.elts
            if isinstance(elt, ast.Tuple)
            and len(elt.elts) == 3
            and isinstance(elt.elts[1], ast.Constant)
            and isinstance(elt.elts[1].value, str)
        }
        assert len(yielded) >= 3, (
            f"only parsed {sorted(yielded)} from claim_next_run; the matcher has "
            "probably stopped matching the claim loop rather than the loop having shrunk"
        )
        for phase_set_name in ("PHASES_SEEING_APPLY_ONLY", "PHASES_SEEING_CONFIGURE_ONLY"):
            phases = getattr(variable_service, phase_set_name)
            unreachable = phases - yielded
            assert not unreachable, (
                f"{phase_set_name} names {sorted(unreachable)}, which no claim in "
                f"claim_next_run yields (it yields {sorted(yielded)}) -- so a variable "
                "in that slot would be withheld from every run and delivered to none"
            )

    async def test_an_unknown_phase_still_excludes_apply_only(self, seed):
        """Fail-closed stays the default for anything not named: withholding a
        credential is recoverable, handing one to a phase that runs pull-request
        code is not."""
        seed(workspace=self.PAIR)
        assert await _resolve("some-future-phase") == [("TOKEN", "read-only")]

    async def test_a_phase_blind_call_returns_BOTH_and_collapses_nothing(self, seed):
        seed(workspace=self.PAIR)
        # Redaction's caller. Collapsing here would drop one secret from the list
        # of literals scrubbed from the model prompt, which is why `None` keys on
        # the pair rather than on the key.
        assert await _resolve(None) == [("TOKEN", "read-only"), ("TOKEN", "write")]


class TestThePrecedenceInversionThatMustNotShip:
    """Apply-only resolves WITHIN a tier, never as a pass over the whole chain.

    The tempting implementation is two passes: resolve the ordinary chain, then
    overlay the apply-only rows. That would let a WORKSPACE apply-only row beat a
    PRIORITY variable set's standard row, so a workspace owner could escape a
    platform-mandated value on applies -- inverting the authority order priority
    sets exist to establish, on the apply. This class is the only thing that
    distinguishes the two designs.
    """

    async def test_a_priority_set_standard_row_still_beats_a_workspace_apply_only_row(self, seed):
        seed(
            workspace=(_Row("TOKEN", "ws-apply-only", apply_only=True),),
            priority=(_Row("TOKEN", "priority-standard"),),
        )
        assert await _resolve("apply") == [("TOKEN", "priority-standard")]

    async def test_a_priority_set_apply_only_row_beats_its_own_standard_row(self, seed):
        seed(
            priority=(
                _Row("TOKEN", "priority-standard"),
                _Row("TOKEN", "priority-apply-only", apply_only=True),
            )
        )
        assert await _resolve("apply") == [("TOKEN", "priority-apply-only")]

    async def test_a_workspace_apply_only_row_still_beats_a_NON_priority_set(self, seed):
        seed(
            workspace=(_Row("TOKEN", "ws-apply-only", apply_only=True),),
            non_priority=(_Row("TOKEN", "set-standard"),),
        )
        assert await _resolve("apply") == [("TOKEN", "ws-apply-only")]


class TestTheDefaultIsUnchanged:
    """With no apply-only row anywhere, resolution must be what it was."""

    async def test_precedence_is_untouched_when_the_feature_is_unused(self, seed):
        seed(
            workspace=(_Row("A", "ws"),),
            priority=(_Row("A", "priority"),),
            non_priority=(_Row("A", "set"),),
        )
        for phase in ("plan", "apply", None):
            assert await _resolve(phase) == [("A", "priority")], phase

    async def test_an_unflagged_variable_reaches_every_phase(self, seed):
        seed(workspace=(_Row("A", "1"),))
        for phase in ("plan", "apply", None):
            assert await _resolve(phase) == [("A", "1")], phase
