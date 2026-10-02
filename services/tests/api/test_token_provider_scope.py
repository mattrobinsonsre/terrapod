"""API-token role resolution must join on (provider, email), not email alone.

GHSA-3m8x-ff8g-7x8c. Both assignment tables are keyed (provider, email), but
resolution queried on email only -- so a token minted after a login at the weakest
configured provider inherited every role assigned to that address under *any*
provider, up to platform admin.

**These tests read the emitted statement.** A mock that returns fixture rows
regardless of the query would pass whether or not the provider clause is there,
which is the exact shape of weak test this repo has shipped before. `_ScopedDB`
compiles each statement and only yields rows when the WHERE actually names the
provider those rows belong to.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.security import HTTPAuthorizationCredentials

from terrapod.api.dependencies import _cache_slot, _resolve_user_roles, get_current_user


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ScopedDB:
    """A db whose `execute` honours the provider in the WHERE clause.

    `assignments` maps provider -> role names. A statement whose compiled SQL does
    not name a provider returns nothing, so dropping the filter yields no roles
    rather than silently yielding every provider's -- and the test still fails,
    because the *expected* provider's roles go missing too.
    """

    def __init__(
        self, assignments: dict[str, list[str]], platform: dict[str, list[str]] | None = None
    ):
        self.assignments = assignments
        self.platform = platform or {}
        self.seen: list[str] = []

    async def execute(self, stmt):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.seen.append(sql)
        table = self.platform if "platform_role_assignments" in sql else self.assignments
        for provider, roles in table.items():
            if f"provider_name = '{provider}'" in sql:
                return _Result([(r,) for r in roles])
        return _Result([])


def _redis(initial: str | None = None):
    r = AsyncMock()
    r.get = AsyncMock(return_value=initial)
    r.set = AsyncMock()
    return r


class TestResolverJoinsOnProvider:
    async def test_a_role_assigned_under_another_provider_is_not_returned(self):
        db = _ScopedDB({"strong-idp": ["platform-admin-ish"]})
        r = _redis()
        with patch("terrapod.redis.client.get_redis_client", return_value=r):
            roles = await _resolve_user_roles(db, "victim@example.com", "weak-idp")
        assert roles == ["everyone"], roles

    async def test_the_matching_provider_does_return_its_roles(self):
        """The negative above would also pass if the resolver returned nothing ever."""
        db = _ScopedDB({"strong-idp": ["deployer"]})
        r = _redis()
        with patch("terrapod.redis.client.get_redis_client", return_value=r):
            roles = await _resolve_user_roles(db, "victim@example.com", "strong-idp")
        assert roles == ["deployer", "everyone"], roles

    async def test_platform_roles_are_provider_scoped_too(self):
        db = _ScopedDB({}, platform={"strong-idp": ["admin"]})
        r = _redis()
        with patch("terrapod.redis.client.get_redis_client", return_value=r):
            weak = await _resolve_user_roles(db, "victim@example.com", "weak-idp")
            strong = await _resolve_user_roles(db, "victim@example.com", "strong-idp")
        assert "admin" not in weak
        assert "admin" in strong

    async def test_both_queries_name_the_provider(self):
        db = _ScopedDB({"p": ["x"]})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            await _resolve_user_roles(db, "u@example.com", "p")
        assert len(db.seen) == 2, db.seen
        for sql in db.seen:
            assert "provider_name" in sql, sql


class TestNoProviderFailsClosed:
    async def test_a_token_with_no_recorded_provider_gets_no_roles(self):
        """A token minted before the column existed cannot be attributed.

        Guessing a provider for it would reinstate the vulnerability, so it
        resolves to `everyone` and nothing else -- it must be re-minted.
        """
        db = _ScopedDB({"local": ["admin"]})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            roles = await _resolve_user_roles(db, "someone@example.com", None)
        assert roles == ["everyone"]
        assert db.seen == [], "no query should be issued for an unattributable principal"

    async def test_no_email_means_no_roles_at_all(self):
        db = _ScopedDB({})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            assert await _resolve_user_roles(db, "", "local") == []


class TestTheCacheIsProviderScoped:
    async def test_a_cached_entry_for_one_provider_is_not_served_to_another(self):
        """The key is the email, so the VALUE has to be a per-provider map.

        Keying the cache by email alone was itself enough to defeat the filter:
        the first provider to resolve would have populated a single list served to
        every other provider for the next 60 seconds.
        """
        cached = json.dumps({_cache_slot("strong-idp", None): ["admin", "everyone"]})
        db = _ScopedDB({"weak-idp": []})
        r = _redis(initial=cached)
        with patch("terrapod.redis.client.get_redis_client", return_value=r):
            roles = await _resolve_user_roles(db, "victim@example.com", "weak-idp")
        assert "admin" not in roles, roles

    async def test_a_cache_hit_for_the_same_provider_is_served(self):
        cached = json.dumps({_cache_slot("strong-idp", None): ["admin", "everyone"]})
        db = _ScopedDB({})
        r = _redis(initial=cached)
        with patch("terrapod.redis.client.get_redis_client", return_value=r):
            roles = await _resolve_user_roles(db, "victim@example.com", "strong-idp")
        assert roles == ["admin", "everyone"]
        assert db.seen == [], "a hit should not query"

    async def test_a_legacy_list_shaped_cache_value_is_discarded_not_read(self):
        """The pre-fix cache held a bare list: the union across providers.

        Reading it would serve exactly the vulnerable answer for up to 60s after
        an upgrade, so the shape is checked rather than trusted.
        """
        db = _ScopedDB({"weak-idp": ["only-this"]})
        r = _redis(initial=json.dumps(["admin", "everyone"]))
        with patch("terrapod.redis.client.get_redis_client", return_value=r):
            roles = await _resolve_user_roles(db, "victim@example.com", "weak-idp")
        assert "admin" not in roles, roles
        assert roles == ["everyone", "only-this"], roles


class TestExternalSsoPolicyAppliesToTokens:
    async def test_a_local_principal_loses_roles_that_require_external_sso(self):
        """The login path refused outright, but only ever saw the session's roles.

        A token minted by a local account carried the restricted roles anyway, so
        the policy was advisory in practice.
        """
        db = _ScopedDB({"local": ["admin", "deployer"]})
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=_redis()),
            patch("terrapod.api.dependencies.settings") as st,
        ):
            st.auth.require_external_sso_for_roles = ["admin"]
            roles = await _resolve_user_roles(db, "someone@example.com", "local")
        assert "admin" not in roles, roles
        assert "deployer" in roles

    async def test_an_sso_principal_keeps_them(self):
        db = _ScopedDB({"okta": ["admin"]})
        with (
            patch("terrapod.redis.client.get_redis_client", return_value=_redis()),
            patch("terrapod.api.dependencies.settings") as st,
        ):
            st.auth.require_external_sso_for_roles = ["admin"]
            roles = await _resolve_user_roles(db, "someone@example.com", "okta")
        assert "admin" in roles


class TestTheTokenPathPassesItsProvider:
    """Driving `get_current_user`, because the resolver being correct is no use if
    the caller does not hand it the provider."""

    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_get_current_user_resolves_with_the_tokens_provider(
        self, mock_validate, mock_session
    ):
        token = MagicMock()
        token.bound_to = "victim@example.com"
        token.kind = "interactive"
        token.identity_provider = "weak-idp"
        token.pinned_roles = None
        mock_validate.return_value = token

        request = MagicMock()
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        request.headers = {}
        request.state = MagicMock()

        db = _ScopedDB({"strong-idp": ["admin"]})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            user = await get_current_user(
                request=request,
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="t.tpod.v"),
                db=db,
            )

        assert user.email == "victim@example.com"
        assert "admin" not in user.roles, user.roles
        assert user.identity_provider == "weak-idp"


class _PinnedDB:
    """A db that honours BOTH the provider and the subject predicate in the WHERE.

    `rows` maps (provider, pinned_subject_or_None) -> role names. A statement only
    yields a row's roles if its compiled SQL both names that provider and admits
    that pinning — so removing either clause changes the answer, and a fake that
    returned fixtures regardless would prove nothing about either.
    """

    def __init__(self, rows: dict[tuple[str, str | None], list[str]]):
        self.rows = rows
        self.seen: list[str] = []

    async def execute(self, stmt):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        self.seen.append(sql)
        if "platform_role_assignments" in sql:
            return _Result([])
        out: list[tuple[str]] = []
        for (provider, pinned), roles in self.rows.items():
            if f"provider_name = '{provider}'" not in sql:
                continue
            # An unpinned row is admitted by the `subject IS NULL` disjunct, which is
            # present in both forms of the predicate.
            if pinned is None:
                if "subject IS NULL" in sql:
                    out += [(r,) for r in roles]
            # A pinned row needs its own subject named.
            elif f"subject = '{pinned}'" in sql:
                out += [(r,) for r in roles]
        return _Result(out)


class TestASubjectPinnedAssignmentIsNarrower:
    """An assignment may pin itself to one IdP subject.

    Email is the weak half of an identity: an IdP that lets a user change their
    address, or an operator recycling one, moves a grant to a different human. A
    subject cannot be acquired by acquiring an address.
    """

    async def test_an_unpinned_assignment_matches_any_subject(self):
        db = _PinnedDB({("okta", None): ["deployer"]})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            roles = await _resolve_user_roles(db, "u@example.com", "okta", "sub-anything")
        assert "deployer" in roles, roles

    async def test_an_unpinned_assignment_matches_an_unknown_subject(self):
        """The normal case: operators type an address, not an opaque `sub`."""
        db = _PinnedDB({("okta", None): ["deployer"]})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            roles = await _resolve_user_roles(db, "u@example.com", "okta", None)
        assert "deployer" in roles, roles

    async def test_a_pinned_assignment_matches_only_its_own_subject(self):
        db = _PinnedDB({("okta", "sub-alice"): ["admin-ish"]})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            alice = await _resolve_user_roles(db, "alice@example.com", "okta", "sub-alice")
            impostor = await _resolve_user_roles(db, "alice@example.com", "okta", "sub-bob")
        assert "admin-ish" in alice, alice
        assert "admin-ish" not in impostor, impostor

    async def test_a_pinned_assignment_does_not_match_an_unknown_subject(self):
        """Fail closed: a credential that cannot prove its subject gets the narrow set."""
        db = _PinnedDB({("okta", "sub-alice"): ["admin-ish"]})
        with patch("terrapod.redis.client.get_redis_client", return_value=_redis()):
            roles = await _resolve_user_roles(db, "alice@example.com", "okta", None)
        assert "admin-ish" not in roles, roles

    async def test_the_cache_does_not_leak_a_pinned_grant_across_subjects(self):
        """Two principals can share an address at one provider — that is why pinning exists.

        A provider-only cache slot would serve one of them the other's pinned roles for
        up to 60 seconds.
        """
        cached = json.dumps({_cache_slot("okta", "sub-alice"): ["admin-ish", "everyone"]})
        db = _PinnedDB({})
        r = _redis(initial=cached)
        with patch("terrapod.redis.client.get_redis_client", return_value=r):
            bob = await _resolve_user_roles(db, "alice@example.com", "okta", "sub-bob")
        assert "admin-ish" not in bob, bob
