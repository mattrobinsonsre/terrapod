"""Which run kind gets the prominent button, and what refuses an impossible one
(#2479).

Two validators, split because the two surfaces can answer different halves.
Bulk update judges the TYPE and VOCABULARY from its payload alone -- it has no
single workspace to merge against until it has matched a set -- while every
single-workspace write judges the CROSS-FIELD half as well, over the row the
write produces rather than the fields it submits.

The cross-field tests are a CROSS PRODUCT, derived from `RUN_KIND_REQUIREMENTS`
rather than listed: the property is a relationship between a kind and two
booleans, so a fixture holding one kind and one flag combination passes whichever
way the code is written. That is the lesson the unique-constraint work recorded
and it applies exactly here.
"""

from __future__ import annotations

import ast
import itertools
import pathlib

import pytest

from terrapod.services import workspace_settings as ws

ALL_KINDS = tuple(ws.RUN_KIND_REQUIREMENTS)
FLAGS = tuple(itertools.product((True, False), repeat=2))


class TestTheFormatHalf:
    """`validate_run_kind_format` -- type and vocabulary, no cross-field check."""

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_every_known_kind_is_returned_unchanged(self, kind):
        assert ws.validate_run_kind_format(kind) == kind

    @pytest.mark.parametrize("raw", [None, ""])
    def test_both_spellings_of_no_preference_fold_to_the_empty_string(self, raw):
        """`None` and `""` mean the same thing, so a client clearing the field
        may send either. There is no deployment-wide default run kind to
        inherit, which is why this is NOT the `engine_version_attr` rule where
        `""` means "the deployment's default" and only an absent key falls
        through."""
        assert ws.validate_run_kind_format(raw) == ""

    @pytest.mark.parametrize("raw", [True, 1, 1.5, ["plan"], {"kind": "plan"}, object()])
    def test_a_non_string_is_refused_as_a_type_error(self, raw):
        with pytest.raises(ValueError, match="must be a string"):
            ws.validate_run_kind_format(raw)

    def test_a_stringified_boolean_is_refused_by_the_vocabulary(self):
        """`True` is refused by the type check above; `"true"` has to be refused
        by the vocabulary, because a JSON client that stringifies its booleans
        sends the second."""
        with pytest.raises(ValueError, match="must be empty or one of"):
            ws.validate_run_kind_format("true")

    @pytest.mark.parametrize("raw", ["apply", "PLAN", "plan-apply", "full_run", "configure "])
    def test_a_word_outside_the_vocabulary_is_refused_and_the_message_lists_it(self, raw):
        with pytest.raises(ValueError) as e:
            ws.validate_run_kind_format(raw)
        for kind in ALL_KINDS:
            assert f"'{kind}'" in str(e.value), f"the refusal does not name {kind}"

    @pytest.mark.parametrize("raw", [" plan", "plan ", "\tfull", "configure\n", "  "])
    def test_whitespace_is_REFUSED_rather_than_stripped(self, raw):
        """Validate, never normalise.

        A Terraform provider writes the server's response back into state, so a
        value the server tidied makes the plan disagree with the result and
        fails the apply as "Provider produced inconsistent result after apply"
        -- the v1.9.0 advisory class. This validator used to `.strip()`, which
        is exactly that defect in its most innocent-looking form: `" plan"` is
        not a value anyone means, and silently storing `"plan"` for it is still
        storing something the caller did not write.

        `"  "` is refused with the rest. A caller meaning "no preference" sends
        `""` or omits the field, and being told so beats having blank space
        quietly accepted as one of the two spellings.
        """
        with pytest.raises(ValueError, match="must be empty or one of"):
            ws.validate_run_kind_format(raw)


class TestTheCrossFieldHalf:
    """`validate_default_run_kind` -- the kind against the RESULTING flags."""

    @pytest.mark.parametrize("plan_apply,configure", FLAGS)
    def test_no_preference_is_accepted_whatever_the_operations_are(self, plan_apply, configure):
        """Including both false.

        That combination is refused, but by `validate_operations` -- this
        validator must not duplicate the check, or the two would answer
        differently the first time one of them moved.
        """
        assert ws.validate_default_run_kind("", plan_apply=plan_apply, configure=configure) == ""

    @pytest.mark.parametrize("kind,flags", list(itertools.product(ALL_KINDS, FLAGS)))
    def test_the_cross_product_of_every_kind_and_every_flag_pair(self, kind, flags):
        """The whole table at once, with the expectation DERIVED from
        `RUN_KIND_REQUIREMENTS` rather than written out.

        A listed expectation is a second copy of the rule, which is how the two
        come to disagree; and a fixture holding one kind cannot see whether the
        flags are consulted at all.
        """
        plan_apply, configure = flags
        have = {"plan_apply": plan_apply, "configure": configure}
        needs = ws.RUN_KIND_REQUIREMENTS[kind]
        missing = [op for op in needs if not have[op]]

        if not missing:
            assert (
                ws.validate_default_run_kind(kind, plan_apply=plan_apply, configure=configure)
                == kind
            )
            return

        with pytest.raises(ValueError) as e:
            ws.validate_default_run_kind(kind, plan_apply=plan_apply, configure=configure)
        detail = str(e.value)
        assert f"'{kind}'" in detail, "the refusal does not name the kind that was refused"
        for op in missing:
            assert f"{op.replace('_', '-')}-enabled" in detail, (
                f"the refusal does not name {op}-enabled, which is the flag the operator has to set"
            )
        for op in needs:
            if op not in missing:
                assert f"{op.replace('_', '-')}-enabled" not in detail, (
                    f"the refusal names {op}-enabled, which is already on -- a "
                    "refusal that lists a satisfied requirement sends the operator "
                    "to change something that is already right"
                )

    def test_a_kind_needing_both_names_both_when_both_are_off(self):
        """`full` is the only kind with two requirements, and the message has to
        carry both or the operator sets one and is refused again."""
        with pytest.raises(ValueError) as e:
            ws.validate_default_run_kind("full", plan_apply=False, configure=False)
        assert "plan-apply-enabled and configure-enabled" in str(e.value)

    @pytest.mark.parametrize("plan_apply,configure", FLAGS)
    def test_it_delegates_the_format_half_rather_than_restating_it(self, plan_apply, configure):
        """An unknown word is refused here too, by the same message, because the
        cross-field validator calls the format one rather than carrying its own
        vocabulary."""
        with pytest.raises(ValueError, match="must be empty or one of"):
            ws.validate_default_run_kind("deploy", plan_apply=plan_apply, configure=configure)


class TestTheVocabularyHasOneProducer:
    """`RUN_KIND_REQUIREMENTS` is the only list of the valid kinds.

    A second list is how two surfaces come to disagree about what is valid, and
    the recorded shape of that failure is a validator and a UI that each know
    four kinds and not the same four. So this walks the modules that judge a run
    kind and asserts none of them writes the vocabulary out again.
    """

    #: Every module that validates or sweeps a run kind, with the kind literals
    #: it is allowed to mention. Derived subjects beat a hand-kept list, but
    #: there is nothing to derive from here -- the question is about absence.
    MODULES = (
        "terrapod/services/workspace_settings.py",
        "terrapod/api/routers/workspace_bulk.py",
        "terrapod/api/routers/tfe_v2.py",
        "terrapod/api/routers/autodiscovery_rules.py",
    )

    def _source(self, rel: str) -> str:
        here = pathlib.Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / rel
            if candidate.exists():
                return candidate.read_text()
        raise AssertionError(f"could not find {rel} from {here}")

    def test_no_module_but_the_producer_lists_the_kinds(self):
        """A module may name ONE kind in passing -- a comment, a single branch --
        but a module containing every kind as a literal has restated the
        vocabulary."""
        for rel in self.MODULES:
            src = self._source(rel)
            tree = ast.parse(src)
            literals = {
                n.value
                for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            named = {k for k in ALL_KINDS if k in literals}
            if rel.endswith("workspace_settings.py"):
                assert named == set(ALL_KINDS), (
                    "the producer no longer holds the kinds as literals; this gate "
                    "is comparing against nothing"
                )
                continue
            assert named != set(ALL_KINDS), (
                f"{rel} names every run kind as a string literal, so it carries a "
                "second copy of the vocabulary. Read RUN_KIND_REQUIREMENTS instead."
            )

    def test_the_requirements_name_only_the_two_real_operations(self):
        """A requirement naming an operation no workspace column holds would
        refuse every kind for ever, and the refusal would name a flag an
        operator cannot set."""
        for kind, needs in ws.RUN_KIND_REQUIREMENTS.items():
            assert needs, f"{kind} requires nothing, so it can never be refused"
            assert set(needs) <= {"plan_apply", "configure"}, (
                f"{kind} requires {needs}, which names something other than the two "
                "operation flags a workspace actually has"
            )
