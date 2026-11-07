"""The run's `(is_configure, configure_after_apply)` pair, and who decides it.

`resolve_configure_shape` is the one producer, called from `create_run` alone so
that all fifteen run creators get the same answer without knowing the rule --
and it had no tests of its own. Its only appearance in the suite was a
POSITIONAL assertion in `test_ha_write_gate.py` ("the leader check happens
before this is called"), which says nothing about what it returns.

That mattered when #2413 F12 gave it a fourth input. The composite came solely
from a standing workspace setting, so asking for one run of apply-then-configure
meant changing behaviour for every later apply including CLI ones, and then
changing it back. The per-run request is a TRISTATE, and the property under test
is a RELATIONSHIP between the request and the workspace setting -- so it is
tested as a cross product. A fixture with one of them passes whichever way the
code is written.
"""

import pytest

from terrapod.services.run_service import resolve_configure_shape


class _Workspace:
    """Only the attribute the producer reads."""

    def __init__(self, configure_after_apply: bool) -> None:
        self.configure_after_apply = configure_after_apply


DECLARES = _Workspace(True)
DOES_NOT = _Workspace(False)


class TestTheCrossProduct:
    """Every (request, workspace) pair, which is the only shape that pins it."""

    @pytest.mark.parametrize(
        ("ws", "requested_after_apply", "expected"),
        [
            # Nothing asked: the workspace decides. The pre-F12 behaviour, and
            # what the CLI and the VCS poller rely on -- neither can send a flag.
            (DECLARES, None, (True, True)),
            (DOES_NOT, None, (False, False)),
            # Asked for: a composite for THIS run, whatever the workspace says.
            # The second row is the gap F12 exists to close.
            (DECLARES, True, (True, True)),
            (DOES_NOT, True, (True, True)),
            # Declined: a plain apply for THIS run. The second row is the half
            # that is easy to miss and was asked for in as many words -- "I
            # still want a Plan+apply on workspaces which have configure so
            # users have the option of just running the first 2 phases."
            (DOES_NOT, False, (False, False)),
            (DECLARES, False, (False, False)),
        ],
    )
    def test_an_apply_resolves_from_the_request_then_the_workspace(
        self, ws, requested_after_apply, expected
    ):
        assert (
            resolve_configure_shape(
                ws,
                requested_configure=False,
                requested_after_apply=requested_after_apply,
                plan_only=False,
                is_destroy=False,
            )
            == expected
        )

    def test_an_explicit_decline_beats_a_workspace_that_declares_it(self):
        """Stated on its own because it is the one direction a boolean could
        not express: absent and false would be the same value, and the
        workspace setting would win."""
        assert resolve_configure_shape(
            DECLARES,
            requested_configure=False,
            requested_after_apply=False,
            plan_only=False,
            is_destroy=False,
        ) == (False, False)
        # The contrast, from the same workspace: absent is NOT a decline.
        assert resolve_configure_shape(
            DECLARES,
            requested_configure=False,
            requested_after_apply=None,
            plan_only=False,
            is_destroy=False,
        ) == (True, True)


class TestTheTwoRequestsTogether:
    """`is-configure` and `configure-after-apply` are not in conflict.

    `is-configure` means "this run carries a configure", which a composite also
    does -- the same reading the run document's own attributes have -- so a
    caller sending both is describing a composite rather than contradicting
    itself.
    """

    def test_a_standalone_configure_is_unchanged(self):
        for ws in (DECLARES, DOES_NOT):
            assert resolve_configure_shape(
                ws,
                requested_configure=True,
                requested_after_apply=None,
                plan_only=False,
                is_destroy=False,
            ) == (True, False), "an explicit configure is STANDALONE, whatever the workspace says"

    def test_both_together_describe_a_composite(self):
        assert resolve_configure_shape(
            DOES_NOT,
            requested_configure=True,
            requested_after_apply=True,
            plan_only=False,
            is_destroy=False,
        ) == (True, True)

    def test_a_configure_request_with_an_explicit_decline_stays_standalone(self):
        """The decline is about the composite, not about the configure: asking
        to configure now and declining to chain it to an apply is coherent, and
        is exactly a standalone."""
        assert resolve_configure_shape(
            DECLARES,
            requested_configure=True,
            requested_after_apply=False,
            plan_only=False,
            is_destroy=False,
        ) == (True, False)


class TestTheExclusions:
    """Both are about there being no apply for a configure to follow."""

    @pytest.mark.parametrize("plan_only,is_destroy", [(True, False), (False, True), (True, True)])
    def test_a_workspace_setting_is_not_honoured_without_an_apply(self, plan_only, is_destroy):
        assert resolve_configure_shape(
            DECLARES,
            requested_configure=False,
            requested_after_apply=None,
            plan_only=plan_only,
            is_destroy=is_destroy,
        ) == (False, False)

    @pytest.mark.parametrize("plan_only,is_destroy", [(True, False), (False, True)])
    def test_an_explicit_request_is_NOT_silently_dropped_here(self, plan_only, is_destroy):
        """The producer honours it and the ROUTER refuses it with a 422 naming
        the reason.

        Deliberate, and the direction matters: resolving it to something smaller
        here would accept the attribute and ignore it, which is the #2116 class
        -- the caller asked for an apply they are not going to get, and nothing
        would say so. A refusal is legible; a silent downgrade is not.
        """
        assert resolve_configure_shape(
            DECLARES,
            requested_configure=False,
            requested_after_apply=True,
            plan_only=plan_only,
            is_destroy=is_destroy,
        ) == (True, True)

    def test_an_explicit_decline_needs_no_exclusion(self):
        """It asks for less rather than more, so there is nothing to refuse."""
        for plan_only, is_destroy in ((True, False), (False, True)):
            assert resolve_configure_shape(
                DECLARES,
                requested_configure=False,
                requested_after_apply=False,
                plan_only=plan_only,
                is_destroy=is_destroy,
            ) == (False, False)
