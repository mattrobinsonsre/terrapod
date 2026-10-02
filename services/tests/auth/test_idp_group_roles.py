"""An IdP group name is not an authorization (GHSA-22vg-4g2w-7w34).

Two separate defects, and only the second one protects a default deployment:

- `role_prefixes` was not a filter. It stripped a matching prefix and passed
  everything else through, so configuring it to scope which groups Terrapod
  listens to had the opposite effect — `terrapod-admin` became `admin`, and an
  unrelated group called `admin` became `admin` too.
- Nothing stopped an IdP group from yielding a platform role. `role_prefixes` is
  empty by default, so in most deployments the filter above does nothing at all
  and this is the rule that closes it.

The floor is tested through `process_login`, not by calling a helper: the defect
was that the real resolution path unioned `identity.groups` verbatim, so a test
that asked a predicate instead would pass while the path stayed broken.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.idp_groups import roles_from_idp_groups
from terrapod.auth.sso import AuthenticatedIdentity
from terrapod.services.sso_service import process_login


class TestRolePrefixesAreAFilter:
    def test_no_prefixes_configured_passes_every_group_through(self):
        """The documented behaviour for an IdP whose groups are already role names.
        Changing this would silently revoke roles on upgrade for every deployment
        that never configured a prefix, which is most of them."""
        assert roles_from_idp_groups(["dev", "admin"], []) == ["dev", "admin"]

    def test_a_matching_prefix_is_stripped(self):
        assert roles_from_idp_groups(["terrapod-dev"], ["terrapod-"]) == ["dev"]

    def test_a_group_matching_no_prefix_is_DROPPED_not_passed_through(self):
        """The fix. Previously `sales` came back as the role `sales`."""
        assert roles_from_idp_groups(["terrapod-dev", "sales"], ["terrapod-"]) == ["dev"]

    def test_a_bare_group_named_like_a_role_is_dropped(self):
        """The attack, in its simplest form: a directory that happens to contain a
        group called `admin`, which Terrapod was reading as its own admin role."""
        assert roles_from_idp_groups(["admin", "audit"], ["terrapod-"]) == []

    def test_the_first_matching_prefix_wins(self):
        out = roles_from_idp_groups(["a-b-dev"], ["a-", "a-b-"])
        assert out == ["b-dev"], "prefixes are tried in order, not longest-first"

    def test_an_empty_group_list_is_not_an_error(self):
        assert roles_from_idp_groups([], ["terrapod-"]) == []


class TestPlatformRolesAreRefusedFromAnIdpGroup:
    @pytest.fixture
    def db(self):
        return AsyncMock(spec=AsyncSession)

    @staticmethod
    def _assignments(db, role_rows: list[str], platform_rows: list[str]) -> None:
        role_result = MagicMock()
        role_result.scalars.return_value.all.return_value = role_rows
        platform_result = MagicMock()
        platform_result.scalars.return_value.all.return_value = platform_rows
        db.execute.side_effect = [role_result, platform_result]

    @staticmethod
    def _identity(groups: list[str], claims: dict | None = None) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            provider_name="oidc",
            subject="user-123",
            email="user@example.com",
            display_name="User",
            groups=groups,
            raw_claims=claims or {},
        )

    @patch("terrapod.services.sso_service.mark_user_seen")
    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_an_idp_group_named_admin_does_not_grant_admin(self, _rec, _mark, db):
        self._assignments(db, [], [])
        result = await process_login(
            db=db, identity=self._identity(["admin", "dev"]), claims_rules=[]
        )
        assert "admin" not in result.roles, "an IdP group granted platform admin"
        # The non-platform group is unaffected: this refuses two names, not a source.
        assert "dev" in result.roles

    @patch("terrapod.services.sso_service.mark_user_seen")
    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_an_idp_group_named_audit_does_not_grant_audit(self, _rec, _mark, db):
        self._assignments(db, [], [])
        result = await process_login(db=db, identity=self._identity(["audit"]), claims_rules=[])
        assert "audit" not in result.roles

    @patch("terrapod.services.sso_service.mark_user_seen")
    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_the_refusal_is_logged_with_what_to_do_instead(self, _rec, _mark, db):
        """Silently dropping a role an operator believes they granted is how this
        becomes a support ticket about Terrapod being broken."""
        self._assignments(db, [], [])
        with patch("terrapod.services.sso_service.logger.warning") as warn:
            await process_login(db=db, identity=self._identity(["admin"]), claims_rules=[])
        calls = [c for c in warn.call_args_list if "Refused platform roles" in str(c)]
        assert calls, "a refused platform role was dropped with nothing in the log"
        assert calls[0].kwargs["refused"] == ["admin"]
        assert "claims_to_roles" in calls[0].kwargs["detail"]

    @patch("terrapod.services.sso_service.mark_user_seen")
    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_a_platform_role_assignment_still_grants_admin(self, _rec, _mark, db):
        """Source 3. The point is to refuse an IdP's *naming*, not to make admin
        ungrantable — if this failed, the fix would lock operators out."""
        self._assignments(db, [], ["admin"])
        result = await process_login(db=db, identity=self._identity([]), claims_rules=[])
        assert "admin" in result.roles

    @patch("terrapod.services.sso_service.mark_user_seen")
    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_a_claims_to_roles_rule_still_grants_admin(self, _rec, _mark, db):
        """Source 2 — the deliberate path, written by whoever administers Terrapod
        rather than whoever administers the directory."""
        from terrapod.config import ClaimsToRolesMapping

        self._assignments(db, [], [])
        rule = ClaimsToRolesMapping(claim="groups", value="platform-engineering", roles=["admin"])
        result = await process_login(
            db=db,
            identity=self._identity([], {"groups": ["platform-engineering"]}),
            claims_rules=[rule],
        )
        assert "admin" in result.roles


class TestBothConnectorsUseTheSharedMapper:
    """The original defect was asymmetry: OIDC applied `role_prefixes` and SAML did
    not apply it at all. A test that only exercised OIDC would have passed before the
    fix and would pass again if SAML drifted back, so the invariant is that neither
    connector derives roles by itself."""

    @pytest.mark.parametrize("module", ["oidc", "saml"])
    def test_the_connector_calls_roles_from_idp_groups(self, module):
        mod = __import__(f"terrapod.auth.connectors.{module}", fromlist=["x"])
        src = inspect.getsource(mod)
        assert "roles_from_idp_groups(" in src, (
            f"the {module} connector does not route its groups through the shared "
            "mapper, so role_prefixes does not apply to it"
        )

    @pytest.mark.parametrize("module", ["oidc", "saml"])
    def test_the_connector_does_not_carry_its_own_prefix_logic(self, module):
        mod = __import__(f"terrapod.auth.connectors.{module}", fromlist=["x"])
        src = inspect.getsource(mod)
        assert "_strip_role_prefixes" not in src, (
            f"the {module} connector has its own prefix helper again; one copy "
            "applied the rule and the other did not, which is the whole defect"
        )
