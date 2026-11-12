"""Configure Jobs can be scheduled apart from plan and apply (#2524).

`runners.nodeSelector` / `tolerations` / `affinity` apply to EVERY Job a listener
launches, which cannot express that the phases have different interruption
profiles. A plan or an apply is minutes long and a retried plan is cheap, so
interruptible capacity suits them; a configure can run for an hour and is not
safely restartable, so an interruption leaves hosts half-configured, mid-release,
with maintenance windows open. One was killed 56 minutes in by a spot reclaim.

**Every assertion here is a PAIR** -- the configure Job and a plan Job built from
the same config -- because the two ways to get this wrong are opposites and only
a pair catches both. Asserting the configure Job alone passes just as well when
the override has been applied to every phase, which would move every plan and
apply off the capacity the operator chose for them: the exact outcome #2524 opens
by saying is unacceptable. Asserting the plan alone catches nothing at all.
"""

from __future__ import annotations

import pytest

from terrapod.config import RunnerConfig, RunnerPhaseScheduling
from terrapod.runner.job_template import _phase_scheduling

GENERAL_SELECTOR = {"pool": "general", "karpenter.sh/capacity-type": "spot"}
GENERAL_TOLERATION = {"key": "spot", "operator": "Exists", "effect": "NoSchedule"}
GENERAL_AFFINITY = {
    "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": ["anti"]},
    "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": ["general"]},
}

CONFIGURE_SELECTOR = {"karpenter.sh/capacity-type": "on-demand"}
CONFIGURE_TOLERATION = {"key": "configure", "operator": "Exists", "effect": "NoSchedule"}
CONFIGURE_AFFINITY = {
    "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": ["configure"]}
}

#: Every phase that is not `configure`. The override must reach none of them.
OTHER_PHASES = ["plan", "apply"]


def _cfg(**configure: object) -> RunnerConfig:
    return RunnerConfig(
        node_selector=dict(GENERAL_SELECTOR),
        tolerations=[dict(GENERAL_TOLERATION)],
        affinity=dict(GENERAL_AFFINITY),
        configure=RunnerPhaseScheduling(**configure),
    )


class TestTheOverrideReachesConfigureAndNothingElse:
    def test_the_node_selector_narrows_a_configure_and_leaves_a_plan_alone(self) -> None:
        cfg = _cfg(node_selector=dict(CONFIGURE_SELECTOR))

        configure_selector, _, _ = _phase_scheduling(cfg, "configure")
        assert configure_selector == {
            "pool": "general",
            # The override WINS the collision: this is the whole point, and the
            # general value says `spot`.
            "karpenter.sh/capacity-type": "on-demand",
        }

        for phase in OTHER_PHASES:
            selector, _, _ = _phase_scheduling(cfg, phase)
            assert selector == GENERAL_SELECTOR, (
                f"the configure override reached a {phase} Job, which moves every "
                "plan and apply off the capacity the operator chose for them"
            )

    def test_the_tolerations_are_appended_for_a_configure_only(self) -> None:
        cfg = _cfg(tolerations=[dict(CONFIGURE_TOLERATION)])

        _, configure_tolerations, _ = _phase_scheduling(cfg, "configure")
        assert configure_tolerations == [GENERAL_TOLERATION, CONFIGURE_TOLERATION], (
            "appended, and in that order: the general tolerations are not replaced, "
            "because a toleration only permits a node and dropping one would narrow "
            "placement by a mechanism nobody asked for"
        )

        for phase in OTHER_PHASES:
            _, tolerations, _ = _phase_scheduling(cfg, phase)
            assert tolerations == [GENERAL_TOLERATION]

    def test_the_affinity_merges_at_its_top_level_for_a_configure_only(self) -> None:
        cfg = _cfg(affinity=dict(CONFIGURE_AFFINITY))

        _, _, configure_affinity = _phase_scheduling(cfg, "configure")
        assert configure_affinity == {
            # Replaced -- the override supplied this key.
            "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": ["configure"]},
            # KEPT -- a different Kubernetes field, and an operator overriding node
            # placement has said nothing about their pod anti-affinity.
            "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": ["anti"]},
        }

        for phase in OTHER_PHASES:
            _, _, affinity = _phase_scheduling(cfg, phase)
            assert affinity == GENERAL_AFFINITY


class TestAnAbsentBlockChangesNothing:
    """The property that makes this safe to ship: an operator who sets none of it
    gets the spec they got before it existed."""

    @pytest.mark.parametrize("phase", ["plan", "apply", "configure"])
    def test_an_empty_block_returns_the_general_settings_unchanged(self, phase: str) -> None:
        cfg = _cfg()
        assert _phase_scheduling(cfg, phase) == (
            GENERAL_SELECTOR,
            [GENERAL_TOLERATION],
            GENERAL_AFFINITY,
        )

    def test_the_default_config_carries_an_empty_block_rather_than_none(self) -> None:
        """No `None` to guard, so no reader can forget the guard."""
        assert RunnerConfig().configure == RunnerPhaseScheduling()
        assert _phase_scheduling(RunnerConfig(), "configure") == ({}, [], {})

    def test_the_general_settings_are_not_mutated(self) -> None:
        """A merge that wrote through to the shared config would leak the override
        into every later Job the same listener builds, including plans."""
        cfg = _cfg(
            node_selector=dict(CONFIGURE_SELECTOR),
            tolerations=[dict(CONFIGURE_TOLERATION)],
            affinity=dict(CONFIGURE_AFFINITY),
        )
        _phase_scheduling(cfg, "configure")
        assert cfg.node_selector == GENERAL_SELECTOR
        assert cfg.tolerations == [GENERAL_TOLERATION]
        assert cfg.affinity == GENERAL_AFFINITY


class TestTheBuiltJobSpecCarriesIt:
    """Through the real builder, not just the resolver.

    `_phase_scheduling` being correct says nothing about the Job spec if the call
    site stops consulting it -- the recorded shape of this failure is a helper that
    is tested directly while nothing drives the thing that uses it.
    """

    def _spec(self, phase: str) -> dict:
        from terrapod.engines.terraform import TerraformRunOptions, TerraformStrategy

        from .test_job_template import _runner_config

        cfg = _runner_config()
        cfg.node_selector = dict(GENERAL_SELECTOR)
        cfg.tolerations = [dict(GENERAL_TOLERATION)]
        cfg.affinity = dict(GENERAL_AFFINITY)
        cfg.configure = RunnerPhaseScheduling(
            node_selector=dict(CONFIGURE_SELECTOR),
            tolerations=[dict(CONFIGURE_TOLERATION)],
        )
        return TerraformStrategy().build_job_spec(
            options=TerraformRunOptions(),
            run_id="run-0123456789abcdef",
            phase=phase,
            runner_config=cfg,
            auth_secret_name="tprun-auth",
            env_vars=[],
            terraform_vars=[],
            namespace="terrapod-runners",
        )

    def test_a_configure_job_carries_the_override_and_a_plan_job_does_not(self) -> None:
        configure = self._spec("configure")["spec"]["template"]["spec"]
        plan = self._spec("plan")["spec"]["template"]["spec"]

        assert configure["nodeSelector"]["karpenter.sh/capacity-type"] == "on-demand"
        assert configure["nodeSelector"]["pool"] == "general"
        assert CONFIGURE_TOLERATION in configure["tolerations"]
        assert GENERAL_TOLERATION in configure["tolerations"]

        assert plan["nodeSelector"] == GENERAL_SELECTOR, (
            "a plan Job picked up the configure override -- every plan and apply "
            "would move off the capacity chosen for them"
        )
        assert plan["tolerations"] == [GENERAL_TOLERATION]
