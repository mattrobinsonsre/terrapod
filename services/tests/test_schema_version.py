"""Unit tests for the app <-> schema skew guard (#544)."""

from unittest.mock import AsyncMock, patch

from terrapod.db import schema_version


async def _check(heads, current):
    with (
        patch.object(schema_version, "code_head_revisions", return_value=frozenset(heads)),
        patch.object(schema_version, "db_current_revision", new=AsyncMock(return_value=current)),
    ):
        return await schema_version.schema_is_current()


async def test_current_matches_head_is_ok():
    ok, detail = await _check({"abc123"}, "abc123")
    assert ok is True
    assert detail == "abc123"


async def test_schema_behind_is_not_ok():
    ok, detail = await _check({"newhead"}, "oldrev")
    assert ok is False
    assert "oldrev" in detail and "newhead" in detail


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
