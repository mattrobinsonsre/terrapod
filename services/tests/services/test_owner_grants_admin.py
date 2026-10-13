"""`rbac.owner_grants_admin`: ownership can be made informational (#2042).

A resource's `owner_email` grants the FULL capability set on it at step 4 of
`resolve_capabilities`, and that grant is the only one in the whole function
label RBAC cannot take back: steps 5 and 6 accumulate with `|=` and the single
subtraction is the catalog clamp at step 7. So revoking a principal's roles
leaves their admin on resources they own intact, and no deny label reaches it.

That matters more since per-workspace cloud identity, because admin on a
workspace reaches a cloud role bound to THAT workspace rather than the agent
pool's shared identity — so ownership became a path to a specific cloud role
that label RBAC could not revoke.

The setting defaults TRUE, so every existing deployment is unchanged. What it
is NOT is a fix for a departing employee: deactivating the user already handles
that. It is for a principal who stays active and should lose access to specific
resources.

**Deliberately out of scope: `may_reference_connection`.** A VCS connection's
owner path is a boolean claim to NAME a connection, not a capability grant, and
it has its own four-way claim model plus a documented upgrade constraint (the
workspace-ownership path is kept because removing it would break deployments
that upgraded on it). `test_the_vcs_connection_owner_path_is_untouched` pins
that this was a decision rather than an oversight.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.auth import capabilities as cap
from terrapod.config import settings
from terrapod.services.capability_resolver import resolve_capabilities

OWNER = "owner@example.com"


def _role(name: str, *, level: str = "admin", allow_labels: dict | None = None):
    r = MagicMock()
    r.name = name
    r.allow_names = []
    r.deny_names = []
    r.allow_all = False
    r.allow_labels = allow_labels or {}
    r.deny_labels = {}
    r.capabilities = sorted(
        cap.expand_preset(
            workspace_permission=level,
            pool_permission=None,
            registry_permission=None,
            catalog_permission=None,
        )
    )
    return r


async def _caps(*, user: str, owner: str, roles: list | None = None, labels: dict | None = None):
    return await resolve_capabilities(
        AsyncMock(),
        user,
        [r.name for r in (roles or [])],
        "dns-prod",
        labels or {},
        owner,
        axis="workspace",
        preloaded_roles=roles or [],
    )


class TestTheDefaultIsUnchanged:
    async def test_the_owner_holds_every_capability(self):
        """True by default, so no deployment changes on upgrade."""
        assert settings.rbac.owner_grants_admin is True
        caps = await _caps(user=OWNER, owner=OWNER)
        assert cap.WORKSPACE_SETTINGS in caps
        assert cap.RUN_APPLY in caps

    async def test_a_non_owner_with_no_roles_holds_nothing(self):
        """The contrast, so the test above is not passing via some floor."""
        caps = await _caps(user="other@example.com", owner=OWNER)
        assert cap.WORKSPACE_SETTINGS not in caps


class TestWhenOwnershipIsInformational:
    @pytest.fixture(autouse=True)
    def _off(self):
        with patch.object(settings.rbac, "owner_grants_admin", False):
            yield

    async def test_the_owner_holds_nothing_without_a_role(self):
        caps = await _caps(user=OWNER, owner=OWNER)
        assert cap.WORKSPACE_SETTINGS not in caps
        assert cap.RUN_APPLY not in caps

    async def test_the_owner_still_gets_what_their_LABELS_grant(self):
        """Ownership stops being a grant; it does not become a denial. This is
        the whole point -- the resource is governed by its labels alone."""
        role = _role("dns-team", allow_labels={"team": ["dns"]})
        caps = await _caps(user=OWNER, owner=OWNER, roles=[role], labels={"team": "dns"})
        assert cap.WORKSPACE_SETTINGS in caps

    async def test_revoking_the_role_now_revokes_the_access(self):
        """The property the setting exists for, stated as the before/after it
        is: with the grant on, this same call still returns admin."""
        caps_without = await _caps(user=OWNER, owner=OWNER, roles=[], labels={"team": "dns"})
        assert cap.WORKSPACE_SETTINGS not in caps_without

        with patch.object(settings.rbac, "owner_grants_admin", True):
            caps_with = await _caps(user=OWNER, owner=OWNER, roles=[], labels={"team": "dns"})
        assert cap.WORKSPACE_SETTINGS in caps_with, (
            "with the grant on, ownership alone must still confer admin"
        )

    async def test_the_everyone_floor_is_untouched(self):
        """Step 6 is a separate grant and the setting must not reach it."""
        caps = await _caps(user=OWNER, owner=OWNER, labels={"access": "everyone"})
        assert cap.WORKSPACE_READ in caps
        assert cap.WORKSPACE_SETTINGS not in caps


class TestTheGrantCannotBeReintroducedElsewhere:
    """The setting is one `and` on one line, so a second owner branch added
    later would bypass it silently -- and the resolver is the kind of code
    somebody extends. Derived from the source rather than listed, so a new
    branch is in scope automatically."""

    def test_every_owner_capability_grant_honours_the_setting(self):
        mod = pathlib.Path(
            __import__("terrapod.services.capability_resolver", fromlist=["x"]).__file__
        )
        tree = ast.parse(mod.read_text())
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            if "owner_email" not in test_src or "user_email" not in test_src:
                continue
            checked += 1
            assert "owner_grants_admin" in test_src, (
                "an owner-identity branch that does not consult "
                f"`rbac.owner_grants_admin`: {test_src}"
            )
        assert checked >= 1, "found no owner-identity branch at all; has it moved?"

    def test_the_vcs_connection_owner_path_is_untouched(self):
        """A decision, not an oversight -- see this module's docstring. If the
        setting is ever extended to cover it, delete this test deliberately
        rather than letting it fail into silence."""
        src = pathlib.Path(
            __import__("terrapod.services.vcs_connection_rbac", fromlist=["x"]).__file__
        ).read_text()
        assert "owner_email" in src, "the VCS owner path has moved; re-check this decision"
        assert "owner_grants_admin" not in src
