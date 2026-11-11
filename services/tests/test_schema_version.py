"""Unit tests for the app <-> schema skew guard (#544, #2500).

Behind and ahead are a *relationship* between the applied revision and this
code's revision graph, not a property of the revision alone, so every fixture
here says which graph the revision sits in. The original fixture did not: it
patched only the head set, so `_check({"newhead"}, "oldrev")` meant "behind" by
intent and "a revision we have never heard of" in fact -- indistinguishable from
ahead, and it would have passed whichever way the code was written.
"""

from unittest.mock import AsyncMock, patch

from terrapod.db import schema_version


async def _check(heads, current, *, known=None, contractions=()):
    """Drive `schema_is_current` with a stated graph.

    `known` is every revision this code ships; it defaults to the heads plus
    whatever the caller named, so a `current` outside it is genuinely unknown to
    us (ahead) and one inside it is genuinely ours but unreached (behind).
    """
    all_revs = frozenset(heads) if known is None else frozenset(known)
    with (
        patch.object(schema_version, "code_head_revisions", return_value=frozenset(heads)),
        patch.object(schema_version, "code_all_revisions", return_value=all_revs),
        patch.object(schema_version, "db_current_revision", new=AsyncMock(return_value=current)),
        patch.object(
            schema_version,
            "applied_contractions",
            new=AsyncMock(return_value=frozenset(contractions)),
        ),
    ):
        return await schema_version.schema_is_current()


async def test_current_matches_head_is_ok():
    ok, detail = await _check({"abc123"}, "abc123", known={"abc123", "older"})
    assert ok is True
    assert detail == "abc123"


async def test_schema_behind_is_not_ok():
    # "oldrev" IS one of ours, and is not the head: the code expects a column
    # the database does not have yet. This is the case #544 exists for.
    ok, detail = await _check({"newhead"}, "oldrev", known={"newhead", "oldrev"})
    assert ok is False
    assert "oldrev" in detail and "newhead" in detail
    assert "BEHIND" in detail


async def test_schema_ahead_is_ok():
    # "futurerev" is unknown to our scripts, so the schema is newer than this
    # code -- the normal state of a rolling upgrade. Staying ready is what keeps
    # the load balancer populated while pods are replaced one at a time (#2500).
    ok, detail = await _check({"myhead"}, "futurerev", known={"myhead", "older"})
    assert ok is True
    assert "ahead" in detail


async def test_the_ahead_message_does_not_claim_the_schema_is_behind():
    # The old wording said "schema at X, code head Y" for both directions, which
    # sent an operator looking for a migration that had in fact succeeded.
    _, detail = await _check({"myhead"}, "futurerev", known={"myhead"})
    assert "behind" not in detail.lower()
    assert "rolling upgrade" in detail


async def test_a_contraction_this_code_predates_is_not_ok():
    # Ahead is only safe while migrations are additive. A contraction recorded
    # by a revision outside our graph means a column we may still read is gone.
    ok, detail = await _check({"myhead"}, "futurerev", known={"myhead"}, contractions={"futurerev"})
    assert ok is False
    assert "CONTRACTING" in detail
    assert "futurerev" in detail


async def test_a_contraction_already_in_our_graph_is_fine():
    # We are at or after it, so the drop it performed is one this code expects.
    ok, detail = await _check(
        {"myhead"}, "futurerev", known={"myhead", "oldcontraction"}, contractions={"oldcontraction"}
    )
    assert ok is True
    assert "ahead" in detail


async def test_missing_alembic_version_row_is_not_ok():
    ok, detail = await _check({"abc"}, None)
    assert ok is False
    assert "not applied" in detail


async def test_unknown_head_does_not_gate_readiness():
    # Scripts unreadable -> we don't block a pod on our own inability to tell.
    ok, detail = await _check(set(), "whatever")
    assert ok is True
    assert "unknown" in detail


def test_code_head_revisions_never_raises():
    # In the test image alembic/ isn't shipped, so this returns empty — but it
    # must always return a frozenset and never raise, whatever the layout.
    assert isinstance(schema_version.code_head_revisions(), frozenset)


def test_code_all_revisions_never_raises():
    assert isinstance(schema_version.code_all_revisions(), frozenset)


async def test_applied_contractions_tolerates_a_missing_table():
    # A schema migrated by a line that predates the table, or a database we
    # cannot reach: both are empty, and neither may raise into `/ready`.
    with patch.object(schema_version, "get_db_session", side_effect=RuntimeError("no such table")):
        assert await schema_version.applied_contractions() == frozenset()
