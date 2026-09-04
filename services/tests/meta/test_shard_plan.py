"""Tests for the integration-suite shard planner (#1468).

Deliberately pure: no database, no fixtures. The planner decides which tests run
on which runner, so a bug here does not fail — it silently skips. That makes it
worth testing more carefully than the code it distributes, and it is the one
piece of this change that can be verified without a container.
"""

from __future__ import annotations

import pytest

from .shard_plan import plan_shards

# Real shape, from a CI run: 28 files, 368 tests, largest 55.
REAL = {
    "test_release_blockers.py": 55,
    "test_run_execution.py": 44,
    "test_deleted_workspace_restore.py": 41,
    "test_role_reach.py": 36,
    "test_workspace_state_lifecycle.py": 18,
    "test_oci_registry_integration.py": 18,
    "test_varset_assignment_rules.py": 17,
    "test_run_state_machine.py": 12,
    "test_rbac_enforcement.py": 12,
    "test_package_cache_integration.py": 11,
    "test_oci_gc_integration.py": 11,
    "test_bootstrap_pools_integration.py": 9,
    "test_provider_publish.py": 9,
    "test_vcs_commit_claim_release.py": 8,
    "test_variables.py": 8,
    "test_multi_pool_dispatch.py": 8,
    "test_replication_lag.py": 8,
    "test_vault_value_source.py": 6,
    "test_replication_outbox.py": 6,
    "test_oci_deletion_integration.py": 6,
    "test_summary_placeholder.py": 5,
    "test_run_task_stage_idempotency.py": 5,
    "test_lifecycle_destroy_retry_integration.py": 3,
    "test_peer_credentials.py": 3,
    "test_ca_init_race.py": 2,
    "test_catalog_lifecycle_integration.py": 2,
    "test_bootstrap_seed_integration.py": 2,
    "test_onboarding_session_integration.py": 2,
}


class TestNothingIsLostOrDuplicated:
    """The two properties that matter, because both failures are silent: a
    dropped file never runs and nobody notices, and a duplicated one wastes a
    shard while hiding the imbalance."""

    @pytest.mark.parametrize("shards", [1, 2, 3, 4, 5, 8, 29])
    def test_every_file_lands_in_exactly_one_shard(self, shards):
        buckets = plan_shards(REAL, shards)
        placed = [f for b in buckets for f in b]

        assert sorted(placed) == sorted(REAL), "a file was dropped or renamed"
        assert len(placed) == len(set(placed)), "a file was placed twice"
        assert len(buckets) == shards

    def test_more_shards_than_files_leaves_empties_rather_than_dropping(self):
        """The planner must not silently discard files it cannot spread. Empty
        shards are the caller's problem to notice; lost tests are nobody's."""
        buckets = plan_shards({"a.py": 1, "b.py": 1}, 5)
        assert sorted(f for b in buckets for f in b) == ["a.py", "b.py"]
        assert sum(1 for b in buckets if not b) == 3


class TestDeterminism:
    """A retried shard must run the same files it ran the first time, whatever
    order the collector happened to produce."""

    def test_the_split_does_not_depend_on_input_order(self):
        reversed_input = dict(reversed(list(REAL.items())))
        assert plan_shards(REAL, 3) == plan_shards(reversed_input, 3)

    def test_equal_counts_are_broken_deterministically(self):
        same = {f"t{i}.py": 5 for i in range(9)}
        assert plan_shards(same, 3) == plan_shards(dict(reversed(list(same.items()))), 3)


class TestBalance:
    """Balance is the entire point — an unbalanced split just moves the tail."""

    @pytest.mark.parametrize("shards,worst", [(2, 190), (3, 128), (4, 98)])
    def test_the_heaviest_shard_stays_near_the_ideal(self, shards, worst):
        buckets = plan_shards(REAL, shards)
        loads = [sum(REAL[f] for f in b) for b in buckets]
        assert max(loads) <= worst, f"{loads} — heaviest shard beyond tolerance"

    def test_a_single_shard_is_the_whole_suite(self):
        assert sorted(plan_shards(REAL, 1)[0]) == sorted(REAL)

    def test_one_dominant_file_cannot_be_split_and_bounds_the_result(self):
        """An honest limit, asserted so it is not mistaken for a packing bug: a
        file is indivisible, so no shard count beats its own weight."""
        counts = {"huge.py": 100, "a.py": 1, "b.py": 1}
        loads = [sum(counts[f] for f in b) for b in plan_shards(counts, 3)]
        assert max(loads) == 100


class TestRejectsNonsense:
    def test_zero_shards_is_refused(self):
        with pytest.raises(ValueError):
            plan_shards(REAL, 0)

    def test_empty_input_yields_empty_shards_rather_than_failing(self):
        assert plan_shards({}, 3) == [[], [], []]
