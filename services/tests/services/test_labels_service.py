"""Tests for the cross-entity labels aggregation service.

Why these tests exist
---------------------
The labels browser surfaces label keys and values across four entity
types (workspaces, agent pools, registry modules, registry providers).
RBAC filtering happens per-entity per-type — and the response shapes
back the UI's three drill-down levels. Regressions here would
silently leak labels the caller shouldn't see, or hide ones they
should.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import labels_service


def _ws(name, labels=None, *, owner=None, id_=None):
    m = MagicMock()
    m.id = id_ or uuid.uuid4()
    m.name = name
    m.labels = labels or {}
    m.owner_email = owner
    return m


def _pool(name, labels=None, *, owner=None, id_=None):
    return _ws(name, labels, owner=owner, id_=id_)


def _module(name, labels=None, *, owner=None, namespace="default", provider="aws", id_=None):
    m = MagicMock()
    m.id = id_ or uuid.uuid4()
    m.name = name
    m.namespace = namespace
    m.provider = provider
    m.labels = labels or {}
    m.owner_email = owner
    return m


def _provider(name, labels=None, *, owner=None, namespace="default", id_=None):
    m = MagicMock()
    m.id = id_ or uuid.uuid4()
    m.name = name
    m.namespace = namespace
    m.labels = labels or {}
    m.owner_email = owner
    return m


def _user(roles=("admin",), email="admin@example.com"):
    u = MagicMock()
    u.email = email
    u.roles = list(roles)
    return u


def _readables(workspaces=(), pools=(), modules=(), providers=()):
    """Context manager that patches all four `_readable_*` helpers.

    Tests the aggregation logic in isolation — RBAC filtering inside
    the readable helpers has its own dedicated tests. Yielding a
    populated set here is equivalent to "the user has read access to
    exactly these entities."
    """
    stack = ExitStack()
    stack.enter_context(
        patch.object(
            labels_service, "_readable_workspaces", AsyncMock(return_value=list(workspaces))
        )
    )
    stack.enter_context(
        patch.object(labels_service, "_readable_pools", AsyncMock(return_value=list(pools)))
    )
    stack.enter_context(
        patch.object(labels_service, "_readable_modules", AsyncMock(return_value=list(modules)))
    )
    stack.enter_context(
        patch.object(labels_service, "_readable_providers", AsyncMock(return_value=list(providers)))
    )
    return stack


class TestAggregateKeys:
    @pytest.mark.asyncio
    async def test_collects_keys_across_all_entity_types(self):
        ws = _ws("alpha", {"account": "prod", "team": "platform"})
        pool = _pool("runner-prod", {"account": "prod"})
        mod = _module("vpc", {"team": "platform"})
        prov = _provider("aws", {"managed-by": "terrapod"})

        with _readables(workspaces=[ws], pools=[pool], modules=[mod], providers=[prov]):
            result = await labels_service.aggregate_keys(MagicMock(), _user())

        keys = {entry["key"] for entry in result}
        assert keys == {"account", "team", "managed-by"}
        # Sorted alphabetically — frontend depends on this for stable display order
        assert [e["key"] for e in result] == sorted(keys)

    @pytest.mark.asyncio
    async def test_value_count_dedupes_per_key(self):
        ws1 = _ws("a", {"env": "prod"})
        ws2 = _ws("b", {"env": "prod"})
        ws3 = _ws("c", {"env": "staging"})

        with _readables(workspaces=[ws1, ws2, ws3]):
            result = await labels_service.aggregate_keys(MagicMock(), _user())

        env_entry = next(e for e in result if e["key"] == "env")
        # Two workspaces share "prod" — value-count is 2 (prod, staging), not 3
        assert env_entry["value-count"] == 2
        assert env_entry["entity-counts"]["workspaces"] == 3

    @pytest.mark.asyncio
    async def test_entity_counts_break_down_by_type(self):
        ws = _ws("a", {"account": "prod"})
        pool1 = _pool("p1", {"account": "prod"})
        pool2 = _pool("p2", {"account": "prod"})
        mod = _module("m", {"account": "prod"})

        with _readables(workspaces=[ws], pools=[pool1, pool2], modules=[mod]):
            result = await labels_service.aggregate_keys(MagicMock(), _user())

        entry = next(e for e in result if e["key"] == "account")
        assert entry["entity-counts"] == {
            "workspaces": 1,
            "agent-pools": 2,
            "registry-modules": 1,
            "registry-providers": 0,
        }

    @pytest.mark.asyncio
    async def test_empty_returns_empty_list(self):
        with _readables():
            result = await labels_service.aggregate_keys(MagicMock(), _user())
        assert result == []


class TestAggregateValuesForKey:
    @pytest.mark.asyncio
    async def test_returns_distinct_values_with_counts(self):
        ws1 = _ws("a", {"env": "prod"})
        ws2 = _ws("b", {"env": "prod"})
        ws3 = _ws("c", {"env": "staging"})
        pool = _pool("p", {"env": "prod"})

        with _readables(workspaces=[ws1, ws2, ws3], pools=[pool]):
            result = await labels_service.aggregate_values_for_key(MagicMock(), _user(), "env")

        # Two values: prod (3 entities), staging (1 entity)
        assert {e["value"] for e in result} == {"prod", "staging"}
        prod = next(e for e in result if e["value"] == "prod")
        assert prod["entity-counts"]["workspaces"] == 2
        assert prod["entity-counts"]["agent-pools"] == 1

    @pytest.mark.asyncio
    async def test_unknown_key_returns_empty(self):
        ws = _ws("a", {"env": "prod"})
        with _readables(workspaces=[ws]):
            result = await labels_service.aggregate_values_for_key(
                MagicMock(), _user(), "does-not-exist"
            )
        assert result == []

    @pytest.mark.asyncio
    async def test_other_keys_ignored(self):
        """A workspace with `env: prod` AND `team: platform` shouldn't
        contribute to the `team` key's count when querying `env`."""
        ws = _ws("a", {"env": "prod", "team": "platform"})
        with _readables(workspaces=[ws]):
            result = await labels_service.aggregate_values_for_key(MagicMock(), _user(), "env")
        assert len(result) == 1
        assert result[0]["value"] == "prod"


class TestListEntitiesForLabel:
    @pytest.mark.asyncio
    async def test_groups_results_by_entity_type(self):
        ws = _ws("alpha", {"account": "prod"})
        pool = _pool("runner-prod", {"account": "prod"})
        mod = _module("vpc", {"account": "prod"})

        with _readables(workspaces=[ws], pools=[pool], modules=[mod]):
            result = await labels_service.list_entities_for_label(
                MagicMock(), _user(), "account", "prod"
            )

        assert len(result["workspaces"]) == 1
        assert result["workspaces"][0]["id"].startswith("ws-")
        assert result["workspaces"][0]["name"] == "alpha"

        assert len(result["agent-pools"]) == 1
        assert result["agent-pools"][0]["id"].startswith("apool-")

        assert len(result["registry-modules"]) == 1
        assert result["registry-modules"][0]["id"].startswith("mod-")

        # Empty list still present so frontend can render the section
        assert result["registry-providers"] == []

    @pytest.mark.asyncio
    async def test_value_must_match_exactly(self):
        """`account: prod` must NOT match a query for `account: production`."""
        ws = _ws("a", {"account": "prod"})
        with _readables(workspaces=[ws]):
            result = await labels_service.list_entities_for_label(
                MagicMock(), _user(), "account", "production"
            )
        assert all(v == [] for v in result.values())

    @pytest.mark.asyncio
    async def test_includes_full_labels_so_ui_can_render_badges(self):
        """The entity payload includes the full labels dict, not just
        the queried key — UI renders all of an entity's labels as
        context when a row is shown."""
        ws = _ws("a", {"account": "prod", "team": "platform", "env": "us-east-1"})
        with _readables(workspaces=[ws]):
            result = await labels_service.list_entities_for_label(
                MagicMock(), _user(), "account", "prod"
            )
        assert result["workspaces"][0]["labels"] == {
            "account": "prod",
            "team": "platform",
            "env": "us-east-1",
        }


class TestRBACFiltering:
    """The labels service must only surface labels on entities the
    user has at least `read` on. We check this by stubbing the
    `_readable_*` helpers — they're the RBAC barrier — and asserting
    the aggregation only sees what they return."""

    @pytest.mark.asyncio
    async def test_aggregation_only_sees_readable_entities(self):
        """If `_readable_workspaces` filters out a workspace tagged
        `secret: yes`, that label must not appear in the keys list."""
        readable = _ws("public", {"env": "prod"})
        # A `secret_ws` would exist in the DB but is filtered out by
        # the readable helper — the filter is the RBAC barrier.
        with _readables(workspaces=[readable]):
            result = await labels_service.aggregate_keys(MagicMock(), _user(roles=()))
        keys = {e["key"] for e in result}
        assert keys == {"env"}
        assert "secret" not in keys
