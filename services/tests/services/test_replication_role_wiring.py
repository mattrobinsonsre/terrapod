"""`sync_cycle` must actually call the propagation, after its commit (#1981).

The behavioural tests in `tests/integration/test_replication_role_propagation.py`
drive `_note_identity_before_apply` and `_propagate_identity_changes` directly,
against a real database and Redis. That proves what the helpers do and proves
nothing about whether the delta loop calls them: deleting the call site leaves
every one of them green. This is that guard.

It is positional as well as present. Propagation must run AFTER the commit,
matching the write path — the change has to be durable before anyone is signed
out on the strength of it — and a call that drifted above the commit would still
satisfy a presence check while reintroducing the ordering bug.
"""

import inspect
import re

from terrapod.services import replication_sync


def _cycle_source() -> str:
    return inspect.getsource(replication_sync.sync_cycle)


class TestTheDeltaLoopIsWired:
    def test_the_loop_records_the_before_set(self):
        src = _cycle_source()
        assert "_note_identity_before_apply" in src, (
            "sync_cycle must capture an identity's role set before applying a "
            "delta — after the write there is nothing left to compare (#1981)"
        )

    def test_the_loop_propagates(self):
        src = _cycle_source()
        assert "_propagate_identity_changes" in src, (
            "sync_cycle must carry applied role deltas to this node's sessions; "
            "without it a replicated demotion leaves the follower's sessions "
            "holding the old roles (#1981)"
        )

    def test_the_before_set_is_captured_BEFORE_the_apply(self):
        """Order within the loop, not merely co-presence."""
        src = _cycle_source()
        note = src.index("_note_identity_before_apply")
        apply_call = src.index("_try_apply(db, client, token, event)")
        assert note < apply_call, "the before-set must be read before _try_apply changes the rows"

    def test_propagation_runs_after_THE_batch_commit(self):
        """A call that drifted above the commit would still look wired.

        Anchored on the commit that ends the delta batch — the one after
        `backfill_pending_classes` — NOT on "any commit earlier in the
        function". `sync_cycle` also commits inside its stale-cursor branch, so
        the looser check passes even with propagation moved above the commit
        that matters, which is a presence check wearing positional clothing.
        """
        src = _cycle_source()
        batch_end = src.index("backfill_pending_classes(db, client, token)")
        commit_match = re.search(r"await db\.commit\(\)", src[batch_end:])
        assert commit_match, (
            "the delta batch no longer commits after backfill_pending_classes — "
            "this guard needs rewriting against the new shape"
        )
        batch_commit = batch_end + commit_match.start()
        propagate = src.index("_propagate_identity_changes")
        assert propagate > batch_commit, (
            "propagation must run after the batch commit, as the write path "
            "does: the change has to be durable before anyone is signed out on "
            "the strength of it (#1981)"
        )


class TestBackfillDeliberatelyDoesNotPropagate:
    def test_backfill_class_does_not_propagate(self):
        """Not an oversight — a documented limit, so it is pinned.

        A fresh follower inserts every row, so each identity would read as a
        widening and refresh sessions fleet-wide, and `reconcile_deletions`
        removes in bulk statements that bypass the ORM. Backfill is recovery,
        and the absolute session ceiling bounds what it leaves stale. If this
        ever becomes wrong, change it deliberately and delete this test.
        """
        src = inspect.getsource(replication_sync.backfill_class)
        assert "_propagate_identity_changes" not in src
