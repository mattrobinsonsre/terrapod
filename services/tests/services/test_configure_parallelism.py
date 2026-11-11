"""The configure phase's own concurrency, and that it is not the plan/apply one (#2499).

One `parallelism` served every phase while the resource allocation it converts
into load had been split per phase since #2475 -- `ee_resource_cpu`/`_memory`
exist separately because "one pair cannot size both", and `parallelism`
describes itself as the knob turning "the allocation above" into actual load,
which is two allocations in two containers.

The axis is plan/apply vs configure, NOT engine: terraform's `-parallelism` and
Pulumi's `--parallel` are the same question in the same container, so #1431's
per-engine unification stands.
"""

from dataclasses import replace

import pytest

from terrapod.engines.terraform import TerraformStrategy
from terrapod.runner.phases.configure import _forced_env
from terrapod.runner.runner_config import RunnerConfig
from terrapod.services.parallelism import (
    MAX_PARALLELISM,
    validate_configure_parallelism,
)
from terrapod.services.workspace_settings import effective_configure_parallelism


def _cfg(**over) -> RunnerConfig:
    base = RunnerConfig.from_env(
        {
            "TP_RUN_ID": "run-1",
            "TP_PHASE": "configure",
            "TP_WORKSPACE_ID": "ws-1",
            "TP_API_URL": "http://api",
            "TP_AUTH_TOKEN": "t",
        }
    )
    return replace(base, config_directory="ansible", playbook="site.yml", **over)


class TestTheConfigurePhaseReadsItsOwnSetting:
    """The property the whole issue is about, driven through the real env builder."""

    def test_forks_comes_from_configure_parallelism_not_parallelism(self):
        # Distinct numbers deliberately: equal ones pass whichever field is read,
        # which is how a test for this can say nothing at all.
        env = _forced_env(_cfg(parallelism=4, configure_parallelism=50))
        assert env["ANSIBLE_FORKS"] == "50"

    def test_the_plan_apply_number_does_not_leak_in(self):
        """The pre-fix behaviour, asserted as absent.

        Before #2499 a workspace tuned to `parallelism=2` for a rate-limited
        provider silently configured two hosts at a time, on an execution
        environment sized for far more.
        """
        env = _forced_env(_cfg(parallelism=2, configure_parallelism=64))
        assert env["ANSIBLE_FORKS"] != "2"

    def test_zero_sets_no_forks_at_all(self):
        """`0` is not a value, so the phase leaves ansible's own default alone.

        Reachable only from a runner whose API sent nothing -- the API refuses 0
        -- so this pins the guard rather than a configuration anyone can write.
        """
        env = _forced_env(_cfg(parallelism=10, configure_parallelism=0))
        assert "ANSIBLE_FORKS" not in env


class TestTheResolver:
    """`None` means inherit, and the resolver is the only place that decides."""

    def test_unset_inherits_the_plan_apply_number(self):
        assert effective_configure_parallelism(None, 10) == 10

    def test_a_pinned_value_wins(self):
        assert effective_configure_parallelism(50, 10) == 50

    def test_a_pinned_value_lower_than_the_sibling_still_wins(self):
        """Not `max()` or a truthiness check: the workspace's own figure governs."""
        assert effective_configure_parallelism(2, 64) == 2


class TestTheRule:
    """One rule for both settings, plus `None`."""

    def test_none_is_accepted_as_the_inherit_marker(self):
        assert validate_configure_parallelism(None) is None

    def test_zero_is_refused_so_it_cannot_become_a_second_inherit_marker(self):
        """`0` would be ambiguous: terraform reads `-parallelism=0` as "no limit"."""
        with pytest.raises(ValueError):
            validate_configure_parallelism(0)

    @pytest.mark.parametrize("raw", [-1, MAX_PARALLELISM + 1, "lots", True, 1.5])
    def test_the_shared_bounds_still_apply(self, raw):
        with pytest.raises(ValueError):
            validate_configure_parallelism(raw)

    def test_a_legal_value_round_trips(self):
        assert validate_configure_parallelism(50) == 50
        assert validate_configure_parallelism("50") == 50


class TestTheOptionsBuilderStatesTheFallbackOnce:
    """A lagging API, and a run created before the column existed.

    The run document carries `configure-parallelism: null` for a run created
    before the column, and an API older than #2499 sends no such key at all.
    Both must land on `parallelism` rather than on 0 -- and the fallback lives in
    the options builder, in ONE place, rather than in each phase.
    """

    def test_an_absent_key_falls_back(self):
        opts = TerraformStrategy().options_from_attrs({"parallelism": 7}, "configure")
        assert opts.configure_parallelism == 7

    def test_an_explicit_null_falls_back(self):
        opts = TerraformStrategy().options_from_attrs(
            {"parallelism": 7, "configure-parallelism": None}, "configure"
        )
        assert opts.configure_parallelism == 7

    def test_a_value_is_taken_as_sent(self):
        opts = TerraformStrategy().options_from_attrs(
            {"parallelism": 7, "configure-parallelism": 50}, "configure"
        )
        assert opts.configure_parallelism == 50
        assert opts.parallelism == 7, "the two must not be collapsed onto one number"
