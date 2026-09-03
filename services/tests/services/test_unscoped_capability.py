"""Unscoped resources — the GPG signing-key store (#1300).

Most resources are things *in* an axis: a workspace, a provider, a pool. They
have a name, labels and an owner, and role scoping is expressed against those.

The GPG key store is the axis itself. It has none of them, so the gate resolves
against an empty name / labels / owner — and the ordinary allow rules can never
match that: `allow_names` has nothing to compare against, and `matches_labels`
requires the key to be present on the resource. The consequence was that
`registry:admin` on the GPG endpoints was reachable only by platform admin,
whatever a role had been granted, which made the capability's name a lie.

The rule these pin: a role matches an unscoped resource only when it is itself
unscoped. That is not a technicality — a role granted authority over *some*
providers must not be able to register a signing key, because a key is a trust
anchor for the whole registry and every provider signed by it becomes
installable, including ones outside that role's scope.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.auth import capabilities as cap
from terrapod.services.capability_resolver import resolve_capabilities

pytestmark = pytest.mark.asyncio


def _role(name, *, allow_names=None, allow_labels=None, level="admin"):
    r = MagicMock()
    r.name = name
    r.allow_names = allow_names or []
    r.deny_names = []
    r.allow_all = False
    r.allow_labels = allow_labels or {}
    r.deny_labels = {}
    r.capabilities = sorted(
        cap.expand_preset(
            workspace_permission=None,
            pool_permission=None,
            registry_permission=level,
            catalog_permission=None,
        )
    )
    return r


async def _registry_caps(roles, *, unscoped):
    """Resolve against the empty resource the GPG gate actually passes."""
    return await resolve_capabilities(
        AsyncMock(),
        "publisher@example.com",
        [r.name for r in roles],
        "",
        {},
        "",
        axis="registry",
        preloaded_roles=roles,
        unscoped=unscoped,
    )


class TestUnscopedRolesReachTheStore:
    async def test_an_unscoped_registry_admin_role_resolves(self):
        caps = await _registry_caps([_role("registry-owner")], unscoped=True)
        assert cap.REGISTRY_ADMIN in caps

    async def test_the_same_role_does_not_reach_a_named_resource_it_was_not_granted(self):
        """Sanity: `unscoped` is about the store, not a blanket widening. The
        empty-resource call is the only one that gets this treatment."""
        caps = await resolve_capabilities(
            AsyncMock(),
            "publisher@example.com",
            ["scoped"],
            "some-provider",
            {"team": "y"},
            "",
            axis="registry",
            preloaded_roles=[_role("scoped", allow_labels={"team": ["x"]})],
            unscoped=True,
        )
        assert cap.REGISTRY_ADMIN not in caps

    async def test_an_unscoped_write_role_gets_write_not_admin(self):
        """The unscoped match decides *whether* a role applies, never *what* it
        grants — that still comes from the role's own capabilities."""
        caps = await _registry_caps([_role("publisher", level="write")], unscoped=True)
        assert cap.REGISTRY_WRITE in caps
        assert cap.REGISTRY_ADMIN not in caps


class TestScopedRolesDoNot:
    @pytest.mark.parametrize(
        ("desc", "role"),
        [
            ("label-scoped", _role("team-x", allow_labels={"team": ["x"]})),
            ("name-scoped", _role("aws-only", allow_names=["aws"])),
            ("both", _role("narrow", allow_names=["aws"], allow_labels={"team": ["x"]})),
        ],
    )
    async def test_a_scoped_registry_admin_cannot_register_a_trust_anchor(self, desc, role):
        caps = await _registry_caps([role], unscoped=True)
        assert cap.REGISTRY_ADMIN not in caps, (
            f"a {desc} role was granted authority over a subset of the registry; "
            "a signing key is a trust anchor for all of it"
        )

    async def test_platform_admin_still_reaches_it(self):
        caps = await resolve_capabilities(
            AsyncMock(),
            "admin@example.com",
            ["admin"],
            "",
            {},
            "",
            axis="registry",
            preloaded_roles=[],
            unscoped=True,
        )
        assert cap.REGISTRY_ADMIN in caps


class TestScopedResolutionIsUnchanged:
    """`unscoped` defaults to False everywhere else, so no other gate moves.
    Without this the flag would be a silent widening of every axis."""

    async def test_an_unscoped_role_does_not_match_an_empty_resource_by_default(self):
        caps = await _registry_caps([_role("registry-owner")], unscoped=False)
        assert caps == frozenset()

    @pytest.mark.parametrize("axis", ["workspace", "pool", "catalog"])
    async def test_other_axes_are_untouched_by_the_default(self, axis):
        role = MagicMock()
        role.allow_all = False
        role.name = "broad"
        role.allow_names = []
        role.deny_names = []
        role.allow_labels = {}
        role.deny_labels = {}
        role.capabilities = sorted(cap.axis_all_caps(axis))

        caps = await resolve_capabilities(
            AsyncMock(),
            "u@example.com",
            ["broad"],
            "",
            {},
            "",
            axis=axis,
            preloaded_roles=[role],
        )
        assert caps == frozenset()
