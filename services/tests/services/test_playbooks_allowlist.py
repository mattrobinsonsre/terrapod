"""The workspace's `playbooks` allow-list over the `playbook` run option (#2527).

`default_playbook` is a DEFAULT: a run may name any playbook instead, typed
from memory, and a mistyped name fails only once the run has started. This is
the SET a configure may choose from, so a playbook that must never run against
this workspace -- a node drain, a database upgrade -- is a control rather than
a convention.

Three properties carry it, and each has its own class below because none is
visible from the others:

* **an empty list is OPEN, not closed.** Every workspace that exists has no
  list, so reading absence as "nothing may run" would stop every configure on
  the deployment at the moment of upgrade. This is the one thing a reader can
  get backwards, so it is asserted from both directions.
* **the pair is validated, never one field.** A non-empty list has to contain
  the default, because a bare Configure runs the default -- and neither field
  can answer that on its own. The merge is what is checked, and only when the
  caller touched one of the two.
* **the RUN is refused at the chokepoint**, on the EFFECTIVE playbook rather
  than on the run's own attribute, so the path everybody takes -- a bare
  Configure, which names nothing -- is checked too rather than only the one
  that names a file.

Nothing is normalised anywhere: `validate, never normalise`, because a
Terraform provider writes the server's response back into state and a list
that comes back trimmed, sorted or de-duplicated fails the apply as an
inconsistent result (the v1.9.0 advisory class).
"""

from __future__ import annotations

import inspect
import types

import pytest

from terrapod.services import run_service
from terrapod.services.workspace_settings import (
    MAX_PLAYBOOKS,
    PlaybookNotAllowed,
    reconcile_playbooks,
    require_allowed_playbook,
    validate_playbooks,
)


class TestTheFormatRule:
    def test_a_plain_list_is_stored_byte_for_byte(self):
        got = validate_playbooks(["release.yml", "ops/drain-node.yml"])
        assert got == ["release.yml", "ops/drain-node.yml"]

    def test_nothing_is_trimmed_sorted_or_de_duplicated(self):
        """The three normalisations a provider cannot survive.

        Sorting is pinned by the ORDER coming back unchanged rather than by an
        assertion about sorting, which is the same thing and cannot be written
        another way round.
        """
        assert validate_playbooks(["b.yml", "a.yml"]) == ["b.yml", "a.yml"]
        # Whitespace is REFUSED rather than stripped, which is the other half of
        # the same rule: an accepted value is byte-exact, and an unacceptable
        # one is a 422 naming the field.
        with pytest.raises(ValueError, match="whitespace"):
            validate_playbooks([" release.yml"])

    def test_a_duplicate_is_refused_and_the_entry_is_named(self):
        """Refused rather than collapsed, and the message says WHICH.

        De-duplicating would hand a provider back a shorter list than it sent.
        Refusing is the only other option, and naming the entry is what makes
        it actionable -- an operator who wrote one twice cannot see that in a
        list of forty.
        """
        with pytest.raises(ValueError, match="'release.yml' appears more than once"):
            validate_playbooks(["release.yml", "a.yml", "release.yml"])

    @pytest.mark.parametrize(
        ("bad", "match"),
        [
            ("release.yml", "must be a list"),
            ([1], "must be strings"),
            ([""], "non-empty"),
            (["../escape.yml"], "traversal"),
            (["/abs.yml"], "leading or trailing slash"),
            (["dir/"], "leading or trailing slash"),
        ],
    )
    def test_what_it_refuses(self, bad, match):
        with pytest.raises(ValueError, match=match):
            validate_playbooks(bad)

    def test_none_and_an_empty_list_both_mean_no_restriction(self):
        assert validate_playbooks(None) == []
        assert validate_playbooks([]) == []

    def test_the_cap_binds(self):
        assert validate_playbooks([f"p{i}.yml" for i in range(MAX_PLAYBOOKS)])
        with pytest.raises(ValueError, match=f"maximum {MAX_PLAYBOOKS} entries"):
            validate_playbooks([f"p{i}.yml" for i in range(MAX_PLAYBOOKS + 1)])


class TestThePairIsReconciledAndNotEachFieldAlone:
    def test_a_consistent_pair_is_stored(self):
        got = reconcile_playbooks(
            sent_playbooks=["a.yml", "b.yml"], stored_playbooks=[], sent_default="b.yml"
        )
        assert got == ["a.yml", "b.yml"]

    def test_a_default_outside_the_list_is_refused_naming_the_allowed_values(self):
        with pytest.raises(ValueError) as exc:
            reconcile_playbooks(
                sent_playbooks=["a.yml"], stored_playbooks=[], sent_default="other.yml"
            )
        # Both halves in the message: the offending value AND what is allowed.
        # Either alone leaves the caller guessing at the other.
        assert "'other.yml'" in str(exc.value)
        assert "'a.yml'" in str(exc.value)

    def test_a_list_with_no_default_is_refused(self):
        """The second rule, and it is not the same as the first.

        A list with an EMPTY default is consistent under "is the default in the
        list?" read naively -- `"" not in ["a.yml"]` happens to be true, but a
        reader could easily write the check the other way round and let it
        through. The refusal exists because a bare Configure would then have no
        playbook at all.
        """
        with pytest.raises(ValueError, match="needs a .*default-playbook"):
            reconcile_playbooks(sent_playbooks=["a.yml"], stored_playbooks=[], sent_default="")

    def test_an_empty_list_imposes_no_pair_rule(self):
        """Opening the workspace back up must not be refused for having no default."""
        assert (
            reconcile_playbooks(sent_playbooks=[], stored_playbooks=["a.yml"], sent_default="")
            == []
        )

    def test_a_caller_who_touched_neither_is_not_re_validated(self):
        """The `reconcile_default_run_kind` lesson, applied.

        Re-checking a stored value on every write wedged a client whose schema
        predated the field: a PATCH setting something unrelated got a permanent
        422 naming a field it could not send. Here there is no orphan case to
        drop -- every write that can set either field is checked -- so the rule
        is simply not applied when the caller mentioned neither.
        """
        # A stored pair that WOULD be refused if re-validated.
        got = reconcile_playbooks(
            sent_playbooks=None, stored_playbooks=["a.yml"], sent_default=None
        )
        assert got == ["a.yml"]

    def test_sending_only_the_default_is_checked_against_the_stored_list(self):
        """Narrowing from the other side. A caller moving the default alone can
        still create the contradiction, so the rule has to fire on either."""
        with pytest.raises(ValueError, match="not in playbooks"):
            reconcile_playbooks(
                sent_playbooks=None, stored_playbooks=["a.yml"], sent_default="b.yml"
            )

    def test_sending_only_the_list_is_checked_against_the_stored_default(self):
        got = reconcile_playbooks(
            sent_playbooks=["a.yml", "b.yml"], stored_playbooks=["a.yml"], sent_default="a.yml"
        )
        assert got == ["a.yml", "b.yml"]
        with pytest.raises(ValueError, match="not in playbooks"):
            reconcile_playbooks(
                sent_playbooks=["b.yml"], stored_playbooks=["a.yml"], sent_default="a.yml"
            )


def _ws(*, playbooks, default_playbook=""):
    return types.SimpleNamespace(playbooks=playbooks, default_playbook=default_playbook)


class TestTheRunIsRefused:
    def test_a_playbook_outside_the_list_is_refused_naming_the_allowed_values(self):
        with pytest.raises(PlaybookNotAllowed) as exc:
            require_allowed_playbook(
                _ws(playbooks=["a.yml"], default_playbook="a.yml"),
                is_configure=True,
                playbook="drain.yml",
            )
        assert "'drain.yml'" in str(exc.value)
        assert "'a.yml'" in str(exc.value)

    def test_the_refusal_is_a_value_error_so_a_bare_except_still_catches_it(self):
        """The `PlanApplyNotEnabled` shape: an explicit type a router can
        translate to 422, and a `ValueError` so the platform's own run creators
        keep catching it with the tuple they already have."""
        assert issubclass(PlaybookNotAllowed, ValueError)

    def test_an_allowed_playbook_passes(self):
        require_allowed_playbook(
            _ws(playbooks=["a.yml", "b.yml"], default_playbook="a.yml"),
            is_configure=True,
            playbook="b.yml",
        )

    def test_the_bare_configure_path_is_checked_through_the_default(self):
        """The EFFECTIVE playbook, which is the point.

        A bare Configure sends no `playbook`, so a check on the run's own
        attribute would leave the one path everybody takes unchecked. In
        practice the default always passes, because the write boundary refuses
        a pair that would leave it outside -- so this asserts the function
        states the rule rather than relying on the other one.
        """
        require_allowed_playbook(
            _ws(playbooks=["a.yml"], default_playbook="a.yml"), is_configure=True, playbook=""
        )
        with pytest.raises(PlaybookNotAllowed, match="'rogue.yml'"):
            require_allowed_playbook(
                _ws(playbooks=["a.yml"], default_playbook="rogue.yml"),
                is_configure=True,
                playbook="",
            )

    def test_an_empty_list_is_a_no_op(self):
        """Every workspace, until somebody writes a list."""
        for playbooks in ([], None):
            require_allowed_playbook(
                _ws(playbooks=playbooks), is_configure=True, playbook="anything.yml"
            )

    def test_a_non_configure_run_is_not_checked(self):
        """`playbook` is a configure-only run option -- `_ENGINE_SPECIFIC_RUN_ATTRS`
        refuses it elsewhere -- so a plan or an apply has no playbook to judge."""
        require_allowed_playbook(
            _ws(playbooks=["a.yml"], default_playbook="a.yml"),
            is_configure=False,
            playbook="drain.yml",
        )


class TestItIsEnforcedAtTheChokepoint:
    """The call site, not the function.

    Testing the guard alone leaves every test passing with the call deleted,
    which is plausible and compiles. `create_run` is the only `Run` constructor
    on the live path, so one call there cannot be bypassed and no caller has to
    remember the rule -- and that is a property of the SOURCE, because the
    function sits mid-way through a 300-line constructor with no seam.
    """

    def test_create_run_calls_it(self):
        src = inspect.getsource(run_service.create_run)
        assert "workspace_settings.require_allowed_playbook(" in src

    def test_it_is_handed_the_run_s_own_playbook_argument(self):
        """A presence check is not an application check.

        `require_allowed_playbook(workspace, is_configure=..., playbook="")`
        would pass this file's other tests, never refuse anything a run asked
        for, and look exactly like a working guard.
        """
        import ast

        tree = ast.parse(inspect.getsource(run_service.create_run).lstrip())
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "require_allowed_playbook"
        ]
        assert len(calls) == 1, f"expected exactly one call, found {len(calls)}"
        kwargs = {k.arg: k.value for k in calls[0].keywords}
        assert isinstance(kwargs.get("playbook"), ast.Name), (
            "the guard is not handed the run's `playbook` parameter -- a literal or a "
            "constant there makes it refuse nothing while reading as enforcement"
        )
        assert kwargs["playbook"].id == "playbook"
        assert isinstance(kwargs.get("is_configure"), ast.Name)
        assert kwargs["is_configure"].id == "is_configure"

    def test_the_guard_runs_after_the_operation_declaration_checks(self):
        """Ordering, so the refusal an operator reads is the most fundamental one.

        A workspace that declares no configure operation at all should say so,
        rather than complaining about which playbook was named on an operation
        it does not have.
        """
        src = inspect.getsource(run_service.create_run)
        assert src.index("require_run_operations(") < src.index("require_allowed_playbook(")
