"""A variable is identified by (category, key), not by key alone (#1898).

Category used to be an attribute hanging off a variable rather than part of its
name — enforced by a unique constraint on `(workspace_id, key)` and again here,
where `resolve_variables` layered variable sets and workspace variables into one
dict keyed on `key`.

The consequence this file pins is the one that lost data: a workspace variable
did not *outrank* a variable-set variable of the same key in another category,
it **replaced** it. The environment variable was absent from the run, and
nothing said so. Precedence still applies — priority sets beat workspace
variables beat non-priority sets — but only within a category.

Driven through the real `resolve_variables` with the two collaborators it reads
mocked, because the defect was in how it keyed its accumulator and nothing else.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import variable_service

pytestmark = pytest.mark.asyncio

MOD = "terrapod.services.variable_service"


def _var(key: str, value: str, category: str):
    v = MagicMock()
    v.key, v.value, v.category = key, value, category
    v.structured, v.sensitive, v.value_source = False, False, "static"
    return v


def _set(*variables):
    vs = MagicMock()
    vs.variables = list(variables)
    return vs


async def _resolve(*, priority=(), workspace=(), non_priority=()):
    priority_sets, non_priority_sets = list(priority), list(non_priority)

    # An explicit conditional, not `priority and x or y`: with no priority sets
    # that idiom returns the NON-priority list for the priority layer too, which
    # applies it twice and lets it overwrite the workspace variables. Caught by
    # the precedence tests below, which is what they are for.
    async def varsets(_db, _wid, priority: bool):
        return priority_sets if priority else non_priority_sets

    with (
        patch(f"{MOD}._get_applicable_varsets", varsets),
        patch(f"{MOD}.list_variables", AsyncMock(return_value=list(workspace))),
    ):
        return await variable_service.resolve_variables(AsyncMock(), "ws-1")


def _by_category(resolved) -> dict[str, str]:
    return {r.category: r.value for r in resolved}


class TestTwoCategoriesSharingAKeyAreTwoVariables:
    async def test_a_workspace_variable_no_longer_deletes_a_set_variable(self) -> None:
        """The data-loss case. Before, the run received ONE variable: the
        workspace's. The set's env var was gone — not lower precedence, absent."""
        resolved = await _resolve(
            non_priority=[_set(_var("region", "from-set", "env"))],
            workspace=[_var("region", "from-workspace", "native")],
        )
        assert _by_category(resolved) == {
            "env": "from-set",
            "native": "from-workspace",
        }

    async def test_the_pair_that_motivated_this(self) -> None:
        """An input variable and an environment variable of the same name. Not
        a multi-engine case at all: `region` as both a `native` variable and an
        `env` one is ordinary, and before the fix one silently deleted the
        other on the way to the runner."""
        resolved = await _resolve(
            workspace=[
                _var("region", "eu-west-1", "native"),
                _var("region", "us-east-1", "env"),
            ]
        )
        assert _by_category(resolved) == {
            "native": "eu-west-1",
            "env": "us-east-1",
        }

    async def test_every_category_sharing_one_key_survives(self) -> None:
        resolved = await _resolve(
            workspace=[
                _var("shared", "a", "native"),
                _var("shared", "b", "env"),
                _var("shared", "c", "git_http_auth"),
                _var("shared", "d", "git_ssh_auth"),
            ]
        )
        assert len(resolved) == 4
        assert _by_category(resolved) == {
            "native": "a",
            "env": "b",
            "git_http_auth": "c",
            "git_ssh_auth": "d",
        }


class TestPrecedenceIsUnchangedWithinACategory:
    """The fix must not weaken precedence — only stop it reaching across
    categories. Each of these would pass just as well on the old code, which is
    the point: they pin what must NOT have changed."""

    async def test_a_workspace_variable_still_beats_a_non_priority_set(self) -> None:
        resolved = await _resolve(
            non_priority=[_set(_var("region", "from-set", "native"))],
            workspace=[_var("region", "from-workspace", "native")],
        )
        assert [r.value for r in resolved] == ["from-workspace"]

    async def test_a_priority_set_still_beats_a_workspace_variable(self) -> None:
        resolved = await _resolve(
            workspace=[_var("region", "from-workspace", "native")],
            priority=[_set(_var("region", "from-priority", "native"))],
        )
        assert [r.value for r in resolved] == ["from-priority"]

    async def test_the_full_three_layer_order_still_holds(self) -> None:
        resolved = await _resolve(
            non_priority=[_set(_var("k", "low", "env"))],
            workspace=[_var("k", "mid", "env")],
            priority=[_set(_var("k", "high", "env"))],
        )
        assert [r.value for r in resolved] == ["high"]

    async def test_precedence_applies_per_category_independently(self) -> None:
        """The two rules meeting: one key, two categories, each resolved on its
        own ladder. A single dict keyed on `key` cannot express this at all."""
        resolved = await _resolve(
            non_priority=[_set(_var("k", "env-low", "env"), _var("k", "tf-low", "native"))],
            priority=[_set(_var("k", "env-high", "env"))],
        )
        assert _by_category(resolved) == {
            "env": "env-high",
            "native": "tf-low",
        }


class TestTheIdentityIsAssertedInBothPlacesItMustBe:
    """Resolution is only half of it — the schema is the other, and the two have
    to agree or the database permits what the resolver cannot represent (or the
    reverse, which is how this defect survived)."""

    def test_the_workspace_constraint_carries_the_category(self) -> None:
        from terrapod.db.models import Variable

        cols = {
            tuple(c.name for c in arg.columns)
            for arg in Variable.__table_args__
            if hasattr(arg, "columns") and getattr(arg, "name", "") == "uq_variables_workspace_key"
        }
        assert cols == {("workspace_id", "key", "category")}

    def test_the_variable_set_constraint_carries_it_too(self) -> None:
        from terrapod.db.models import VariableSetVariable

        cols = {
            tuple(c.name for c in arg.columns)
            for arg in VariableSetVariable.__table_args__
            if hasattr(arg, "columns") and getattr(arg, "name", "") == "uq_variable_set_variables"
        }
        assert cols == {("variable_set_id", "key", "category")}
