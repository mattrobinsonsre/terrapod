"""The run document says what KIND of run it is, derived and not stored (#2507, #2509).

A configure-only run was badged **plan + apply** *and* **configure** at once,
and the run page opened on Overview when the configure log was the only log the
run had. One cause behind both: three surfaces each derived "is this
configure-only" from `is-configure`, `configure-after-apply` and the
workspace's `plan-apply-enabled`, and no gate can see three copies of a
predicate disagreeing.

`run_service.run_kind` is the one producer. What is worth asserting about it is
not the three-branch mapping -- that is four lines and obvious -- but three
RELATIONSHIPS, none of which is visible from the function alone:

* it SHARES the workspace's vocabulary (`RUN_KIND_REQUIREMENTS`), so a UI
  reading one and a `default-run-kind` written in the other cannot disagree
  about what a configure is;
* the serializer EMITS it, on both surfaces, beside the two flags it replaces;
* it agrees with `is_standalone_configure`, the predicate the apply-only side
  effects already key on. Two answers to "is this a configure and nothing else"
  that could drift is the defect this file exists to prevent, one layer down.
"""

from __future__ import annotations

import inspect
import types

import pytest

from terrapod.api.routers import runs as runs_router
from terrapod.services import run_service
from terrapod.services.workspace_settings import RUN_KIND_REQUIREMENTS


def _run(*, is_configure: bool, configure_after_apply: bool):
    """The two columns `run_kind` reads, and deliberately nothing else.

    A `SimpleNamespace` rather than a `MagicMock`: an auto-attribute would make
    a mutation that reads a THIRD column pass silently, which is the whole
    shape this function exists not to have.
    """
    return types.SimpleNamespace(
        is_configure=is_configure,
        configure_after_apply=configure_after_apply,
    )


class TestTheDerivation:
    @pytest.mark.parametrize(
        ("is_configure", "after_apply", "expected"),
        [
            (False, False, "plan_apply"),
            # `configure_after_apply` without `is_configure` cannot happen --
            # `resolve_configure_flags` never produces it -- and if it somehow
            # did, the run has no configure, so `plan_apply` is the honest
            # answer rather than a crash or a `full` that nothing will run.
            (False, True, "plan_apply"),
            (True, False, "configure"),
            (True, True, "full"),
        ],
    )
    def test_each_shape_gets_its_word(self, is_configure, after_apply, expected):
        assert (
            run_service.run_kind(_run(is_configure=is_configure, configure_after_apply=after_apply))
            == expected
        )

    def test_every_value_is_in_the_workspace_vocabulary(self):
        """The relationship, not the mapping.

        A second spelling here -- `composite`, say, or `configure_only` -- would
        read identically in isolation and make a UI switching on `run-kind`
        disagree with a `default-run-kind` the operator wrote. The vocabularies
        are shared on purpose, so that is asserted rather than left to a comment.
        """
        produced = {
            run_service.run_kind(_run(is_configure=a, configure_after_apply=b))
            for a in (False, True)
            for b in (False, True)
        }
        assert produced <= set(RUN_KIND_REQUIREMENTS), (
            f"run_kind produced {sorted(produced - set(RUN_KIND_REQUIREMENTS))}, which "
            "`default-run-kind` cannot name -- the two vocabularies have drifted"
        )

    def test_plan_is_never_produced_although_the_vocabulary_has_it(self):
        """`plan` is a workspace's BUTTON preference, not a kind of run.

        Whether a run is speculative is `plan-only`, which the document already
        carries and every consumer already reads. Emitting `plan` here would
        give one fact two spellings, which is how two consumers come to
        disagree -- so the omission is deliberate and pinned.
        """
        assert "plan" in RUN_KIND_REQUIREMENTS
        produced = {
            run_service.run_kind(_run(is_configure=a, configure_after_apply=b))
            for a in (False, True)
            for b in (False, True)
        }
        assert "plan" not in produced

    def test_it_agrees_with_the_standalone_predicate(self):
        """Two answers to one question, held together.

        `is_standalone_configure` is what the apply-only side effects key on --
        run triggers, clearing `state_diverged`, resetting `drift_status` -- and
        it has a SQL twin for the same reason. A `run_kind` that disagreed with
        it would mean the UI and the state machine had different ideas about
        which runs changed infrastructure.
        """
        for a in (False, True):
            for b in (False, True):
                run = _run(is_configure=a, configure_after_apply=b)
                assert (
                    run_service.run_kind(run) == "configure"
                ) == run_service.is_standalone_configure(run)


class TestTheSerializerEmitsIt:
    """The emission, which the derivation's own tests cannot see.

    Source introspection rather than a built response, because `_run_json`
    takes a Run with ~70 serialised columns: a fixture for it is a fixture for
    the whole document, and every other attribute's test in this tree pays that
    cost once in `tests/api`. What is specific to THIS change is that the key
    exists, is spelled the same way the consumers read it, and comes from the
    one producer rather than from an expression written out again.
    """

    def test_the_key_is_emitted_from_the_producer(self):
        src = inspect.getsource(runs_router._run_json)
        assert '"run-kind": run_service.run_kind(run)' in src, (
            "`run-kind` is not emitted from `run_service.run_kind` -- an expression "
            "written out here is the fourth copy of the predicate this replaced"
        )

    def test_it_sits_beside_the_flags_it_replaces(self):
        """Adjacency, so the next reader finds all three together.

        Not cosmetic: the two flags stay on the document for ever (they are on
        the frozen attribute contract and other consumers read them), so what
        stops a fifth surface deriving the kind again is finding the derived
        answer in the same place.
        """
        src = inspect.getsource(runs_router._run_json)
        assert src.index('"is-configure"') < src.index('"run-kind"')
        assert src.index('"run-kind"') < src.index('"playbook"')

    def test_it_is_not_folded_for_the_compatibility_surface(self):
        """`run-kind` goes out on BOTH surfaces, unlike `status`.

        `_reported_run_status` folds `configuring` to `applying` on
        `/api/tfe/v2`, because `go-tfe` switches on a fixed set of statuses.
        `run-kind` needs no such fold: an unknown ATTRIBUTE is ignored by a
        JSON:API client, where an unknown status value falls through its
        `switch`. So it is emitted unconditionally, and a future fold would be
        a change of mind rather than an oversight.
        """
        src = inspect.getsource(runs_router._run_json)
        line = next(ln for ln in src.splitlines() if '"run-kind"' in ln)
        assert "tfe" not in line
