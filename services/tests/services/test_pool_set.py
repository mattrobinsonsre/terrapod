"""The workspace agent-pool set (#1085 / #960 phase 0).

The set is FLAT — element 0 carries no dispatch preference, it is only where
the pre-multi-pool `agent_pool_id` column keeps a value so un-upgraded clients
keep working. These tests pin that split, and the normalisation that stops a
malformed or duplicated id reaching the dispatcher.
"""

import uuid
from types import SimpleNamespace

from terrapod.services import pool_set


def _ws(pool_id=None, extras=None):
    return SimpleNamespace(agent_pool_id=pool_id, agent_pool_extra_ids=extras or [])


class TestWorkspacePoolIDs:
    def test_no_pool_is_empty_set(self):
        assert pool_set.workspace_pool_ids(_ws()) == []

    def test_single_pool_matches_pre_multi_pool_behaviour(self):
        p = uuid.uuid4()
        assert pool_set.workspace_pool_ids(_ws(p)) == [p]

    def test_element_zero_first_then_the_rest(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        assert pool_set.workspace_pool_ids(_ws(a, [str(b), str(c)])) == [a, b, c]

    def test_null_extras_column_tolerated(self):
        # A row written before the column existed reads back as NULL, not [].
        p = uuid.uuid4()
        assert pool_set.workspace_pool_ids(_ws(p, None)) == [p]

    def test_duplicate_of_element_zero_is_dropped(self):
        p = uuid.uuid4()
        assert pool_set.workspace_pool_ids(_ws(p, [str(p)])) == [p]


class TestNormalise:
    def test_accepts_prefixed_and_raw_ids(self):
        p = uuid.uuid4()
        assert pool_set.normalise([f"apool-{p}"]) == [p]
        assert pool_set.normalise([str(p)]) == [p]
        assert pool_set.normalise([p]) == [p]

    def test_drops_blanks_and_garbage(self):
        p = uuid.uuid4()
        assert pool_set.normalise([None, "", "not-a-uuid", p]) == [p]

    def test_preserves_declared_order(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        assert pool_set.normalise([b, a]) == [b, a]
        assert pool_set.normalise([a, b]) == [a, b]

    def test_deduplicates(self):
        p = uuid.uuid4()
        assert pool_set.normalise([p, f"apool-{p}", str(p)]) == [p]


class TestSplit:
    def test_empty_set_clears_both_columns(self):
        assert pool_set.split([]) == (None, [])

    def test_head_and_string_tail(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        head, extras = pool_set.split([a, b])
        assert head == a
        # The tail lands in a JSONB column, so it must be JSON-native strings.
        assert extras == [str(b)]
        assert all(isinstance(e, str) for e in extras)

    def test_round_trips_through_the_workspace_reader(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        head, extras = pool_set.split([a, b, c])
        assert pool_set.workspace_pool_ids(_ws(head, extras)) == [a, b, c]


class TestRunPoolIDs:
    def test_candidate_set_from_snapshot(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        run = SimpleNamespace(pool_id=a, pool_extra_ids=[str(b)])
        assert pool_set.run_pool_ids(run) == [a, b]

    def test_run_without_the_column_still_resolves(self):
        # A Run object built before the column existed (or a lightweight stub).
        a = uuid.uuid4()
        assert pool_set.run_pool_ids(SimpleNamespace(pool_id=a)) == [a]

    def test_unassigned_run_has_no_candidates(self):
        assert pool_set.run_pool_ids(SimpleNamespace(pool_id=None, pool_extra_ids=[])) == []
