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

    def __init__(self, key, value, apply_only=False, category="env", sensitive=False):
        self.key = key
        self.value = value
        self.apply_only = apply_only
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
