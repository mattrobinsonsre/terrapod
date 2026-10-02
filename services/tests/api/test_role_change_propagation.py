"""A role change reaches the credentials that were issued before it.

GHSA-pwrq-j4cv-w7qg. A web session carries the roles login resolved, so a
demotion that only writes the database leaves the demoted user holding `admin` in
the UI — long enough to undo the demotion.

**Every test here drives the route**, with a real session in a Redis fake and the
real `sessions` and `role_change_propagation` code underneath. Calling the
propagation helper directly would pass just as happily against a route that had
stopped calling it, which is the thing this file exists to catch; and asserting
that a mock was awaited would pass against a route that called it with the wrong
arguments. The assertion is always "is the session actually gone from Redis", or
"does it actually carry the new role".

The DB fake evaluates the statement's WHERE clause rather than answering from a
call counter, because several of these properties ARE the filter: `_role_holders`
has to find the holders of *this* role, and the assignment paths have to read the
rows for *this* (provider, email). A fake that ignored the query would assert
nothing about any of it.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.sql.operators import and_, eq

from terrapod.api.dependencies import _TOKEN_ROLES_PREFIX, AuthenticatedUser
from terrapod.api.routers import role_assignments as ra_router
from terrapod.api.routers import roles as roles_router
from terrapod.api.routers import users as users_router
from terrapod.auth import sessions as sessions_module
from terrapod.auth.sessions import create_session, get_session
from terrapod.config import settings
from terrapod.db.models import PlatformRoleAssignment, Role, RoleAssignment, User, now_utc
from terrapod.services import role_change_propagation
from tests.fake_redis import FakeRedis

# ── the fakes ────────────────────────────────────────────────────────────


def _where_pairs(clause) -> list[tuple[str, object]]:
    """Flatten a conjunction of simple `column == literal` tests.

    Everything these routes issue is that shape. Anything else raises rather than
    being silently ignored — a WHERE this cannot read would make the fake answer
    a different question from the one the code asked.
    """
    if clause is None:
        return []
    op = getattr(clause, "operator", None)
    if op is and_:
        pairs: list[tuple[str, object]] = []
        for child in clause.clauses:
            pairs.extend(_where_pairs(child))
        return pairs
    if op is eq:
        return [(clause.left.key, clause.right.value)]
    raise AssertionError(f"the DB fake cannot evaluate this WHERE clause: {clause!r}")


class _Result:
    def __init__(self, rows: list, column_names: list[str] | None) -> None:
        self._rows = rows
        self._columns = column_names

    def scalars(self):
        return self

    def all(self):
        if self._columns is None:
            return list(self._rows)
        return [tuple(getattr(row, name) for name in self._columns) for row in self._rows]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    """A query-aware stand-in for an AsyncSession over a handful of rows."""

    def __init__(self, rows: list) -> None:
        self.rows = list(rows)
        self.committed = 0

    async def execute(self, stmt):
        descriptions = stmt.column_descriptions
        model = descriptions[0]["entity"]
        # Whole-entity selects carry the entity as the single description; a
        # column select names each column, which is what `.all()` has to shape.
        whole_entity = len(descriptions) == 1 and descriptions[0]["name"] == model.__name__
        columns = None if whole_entity else [d["name"] for d in descriptions]

        pairs = _where_pairs(stmt.whereclause)
        matched = [
            row
            for row in self.rows
            if isinstance(row, model) and all(getattr(row, k) == v for k, v in pairs)
        ]
        return _Result(matched, columns)

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, _obj) -> None:
        return None

    def add(self, obj) -> None:
        self.rows.append(obj)

    async def delete(self, obj) -> None:
        self.rows = [row for row in self.rows if row is not obj]


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    """One fake shared by every module that reaches for Redis in these paths."""
    fake = FakeRedis()
    for module in (sessions_module, role_change_propagation):
        monkeypatch.setattr(module, "get_redis_client", lambda: fake)
    monkeypatch.setattr(
        sessions_module,
        "now_utc",
        lambda: now_utc() + timedelta(seconds=fake.offset),
    )
    monkeypatch.setattr(settings.auth, "session_ttl_hours", 12)
    monkeypatch.setattr(settings.auth, "session_absolute_ttl_hours", 24)
    return fake


def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


async def _session_for(email: str, roles: list[str], provider: str = "local"):
    return await create_session(
        email=email,
        display_name=None,
        roles=roles,
        provider_name=provider,
    )


def _token_roles_cached(redis: FakeRedis, email: str) -> bool:
    return (_TOKEN_ROLES_PREFIX + email) in redis.values


async def _cache_token_roles(redis: FakeRedis, email: str) -> None:
    await redis.set(_TOKEN_ROLES_PREFIX + email, json.dumps(["admin"]), ex=60)


def _put_body(email: str, roles: list[str], provider: str = "local") -> dict:
    return {
        "data": {
            "attributes": {
                "provider-name": provider,
                "email": email,
                "roles": roles,
            }
        }
    }


# ── the assignment routes ────────────────────────────────────────────────


class TestSettingRoleAssignments:
    async def test_removing_a_role_ends_the_users_session(self, redis):
        """The headline: a demoted admin does not keep admin in the browser."""
        victim = "demoted@example.com"
        session = await _session_for(victim, ["admin", "everyone"])
        await _cache_token_roles(redis, victim)
        db = FakeDB(
            [PlatformRoleAssignment(provider_name="local", email=victim, role_name="admin")]
        )

        await ra_router.set_role_assignments(body=_put_body(victim, []), user=_admin(), db=db)

        assert await get_session(session.token) is None, (
            "the demoted user's session survived, so they still hold admin in the "
            "web UI for the rest of its lifetime"
        )
        assert not _token_roles_cached(redis, victim)

    async def test_replacing_admin_with_nothing_but_everyone_is_still_a_reduction(self, redis):
        """`everyone` is implicit and never stored, so it must not read as a role
        that was added — which would make a demotion look like a widening."""
        victim = "demoted@example.com"
        session = await _session_for(victim, ["admin", "everyone"])
        db = FakeDB(
            [PlatformRoleAssignment(provider_name="local", email=victim, role_name="admin")]
        )

        await ra_router.set_role_assignments(
            body=_put_body(victim, ["everyone"]), user=_admin(), db=db
        )

        assert await get_session(session.token) is None

    async def test_granting_a_role_keeps_the_session_and_adds_the_role(self, redis):
        """A widening refreshes in place: nobody is logged out for being promoted."""
        promoted = "promoted@example.com"
        session = await _session_for(promoted, ["everyone", "from-idp-group"])
        db = FakeDB([Role(name="deployer", capabilities=[])])

        await ra_router.set_role_assignments(
            body=_put_body(promoted, ["deployer"]), user=_admin(), db=db
        )

        live = await get_session(session.token)
        assert live is not None, "a promotion logged the user out"
        assert "deployer" in live.roles
        # The IdP-derived role is still there: the roles are unioned, not
        # re-resolved from the assignments table.
        assert "from-idp-group" in live.roles

    async def test_a_write_that_both_grants_and_removes_is_a_removal(self, redis):
        victim = "shuffled@example.com"
        session = await _session_for(victim, ["admin", "everyone"])
        db = FakeDB(
            [
                PlatformRoleAssignment(provider_name="local", email=victim, role_name="admin"),
                Role(name="deployer", capabilities=[]),
            ]
        )

        await ra_router.set_role_assignments(
            body=_put_body(victim, ["deployer"]), user=_admin(), db=db
        )

        assert await get_session(session.token) is None

    async def test_another_providers_session_is_left_signed_in(self, redis):
        """Roles come from the assignments for a session's OWN provider, so a
        change to one provider's grants cannot have made another's stale."""
        email = "both@example.com"
        local = await _session_for(email, ["admin"], provider="local")
        okta = await _session_for(email, ["admin"], provider="okta")
        db = FakeDB([PlatformRoleAssignment(provider_name="local", email=email, role_name="admin")])

        await ra_router.set_role_assignments(
            body=_put_body(email, [], provider="local"), user=_admin(), db=db
        )

        assert await get_session(local.token) is None
        assert await get_session(okta.token) is not None

    async def test_an_unchanged_write_leaves_the_session_alone(self, redis):
        email = "steady@example.com"
        session = await _session_for(email, ["admin", "everyone"])
        db = FakeDB([PlatformRoleAssignment(provider_name="local", email=email, role_name="admin")])

        await ra_router.set_role_assignments(body=_put_body(email, ["admin"]), user=_admin(), db=db)

        assert await get_session(session.token) is not None

    async def test_everyone_is_never_written_into_a_session(self, redis):
        """It is implicit and never stored, so it is never in `previous` either —
        counting it as an addition would make every no-op PUT look like a grant
        and would start injecting the role into session records."""
        email = "steady@example.com"
        session = await _session_for(email, ["admin"])
        db = FakeDB([PlatformRoleAssignment(provider_name="local", email=email, role_name="admin")])

        await ra_router.set_role_assignments(
            body=_put_body(email, ["admin", "everyone"]), user=_admin(), db=db
        )

        live = await get_session(session.token)
        assert live is not None
        assert live.roles == ["admin"]


class TestDeletingOneRoleAssignment:
    async def test_the_session_ends(self, redis):
        victim = "demoted@example.com"
        session = await _session_for(victim, ["admin", "everyone"])
        await _cache_token_roles(redis, victim)
        db = FakeDB(
            [PlatformRoleAssignment(provider_name="local", email=victim, role_name="admin")]
        )

        await ra_router.delete_role_assignment(
            provider_name="local",
            email=victim,
            role_name="admin",
            user=_admin(),
            db=db,
        )

        assert await get_session(session.token) is None
        assert not _token_roles_cached(redis, victim)


# ── the role routes ──────────────────────────────────────────────────────


def _patch_body(attrs: dict) -> dict:
    return {"data": {"attributes": attrs}}


def _role_and_holder(email: str, *, capabilities: list[str], **grant):
    role = Role(
        name="deployer",
        description="",
        allow_all=grant.pop("allow_all", False),
        allow_labels=grant.pop("allow_labels", {}),
        allow_names=grant.pop("allow_names", []),
        deny_labels=grant.pop("deny_labels", {}),
        deny_names=grant.pop("deny_names", []),
        capabilities=capabilities,
    )
    assert not grant, grant
    holder = RoleAssignment(provider_name="local", email=email, role_name="deployer")
    return role, holder


class TestUpdatingARolesGrant:
    async def test_removing_a_capability_signs_the_holders_out(self, redis):
        email = "holder@example.com"
        session = await _session_for(email, ["deployer", "everyone"])
        role, holder = _role_and_holder(email, capabilities=["run:apply", "run:plan"])
        db = FakeDB([role, holder])

        await roles_router.update_role(
            role_name="deployer",
            body=_patch_body({"capabilities": ["run:plan"]}),
            user=_admin(),
            db=db,
        )

        assert await get_session(session.token) is None

    async def test_adding_a_capability_does_not(self, redis):
        """Logging a fleet out for a widening is cost with no benefit."""
        email = "holder@example.com"
        session = await _session_for(email, ["deployer", "everyone"])
        role, holder = _role_and_holder(email, capabilities=["run:plan"])
        db = FakeDB([role, holder])

        await roles_router.update_role(
            role_name="deployer",
            body=_patch_body({"capabilities": ["run:plan", "run:apply"]}),
            user=_admin(),
            db=db,
        )

        assert await get_session(session.token) is not None

    async def test_editing_only_the_description_does_not(self, redis):
        email = "holder@example.com"
        session = await _session_for(email, ["deployer", "everyone"])
        role, holder = _role_and_holder(email, capabilities=["run:plan"])
        db = FakeDB([role, holder])

        await roles_router.update_role(
            role_name="deployer",
            body=_patch_body({"description": "renamed"}),
            user=_admin(),
            db=db,
        )

        assert await get_session(session.token) is not None

    async def test_narrowing_the_scope_signs_the_holders_out(self, redis):
        """Adding a deny rule takes access away without touching a capability."""
        email = "holder@example.com"
        session = await _session_for(email, ["deployer", "everyone"])
        role, holder = _role_and_holder(email, capabilities=["run:plan"], allow_all=True)
        db = FakeDB([role, holder])

        await roles_router.update_role(
            role_name="deployer",
            body=_patch_body({"deny-names": ["prod"]}),
            user=_admin(),
            db=db,
        )

        assert await get_session(session.token) is None

    async def test_turning_allow_all_off_signs_the_holders_out(self, redis):
        email = "holder@example.com"
        session = await _session_for(email, ["deployer", "everyone"])
        role, holder = _role_and_holder(email, capabilities=["run:plan"], allow_all=True)
        db = FakeDB([role, holder])

        await roles_router.update_role(
            role_name="deployer",
            body=_patch_body({"allow-all": False}),
            user=_admin(),
            db=db,
        )

        assert await get_session(session.token) is None

    async def test_someone_who_does_not_hold_the_role_stays_signed_in(self, redis):
        bystander = await _session_for("bystander@example.com", ["everyone"])
        role, holder = _role_and_holder("holder@example.com", capabilities=["run:apply"])
        db = FakeDB([role, holder])

        await roles_router.update_role(
            role_name="deployer",
            body=_patch_body({"capabilities": []}),
            user=_admin(),
            db=db,
        )

        assert await get_session(bystander.token) is not None


class TestDeletingARole:
    async def test_the_holders_are_signed_out(self, redis):
        """The assignments cascade away, but the role NAME stays in the session:
        recreate a role under that name and the session gets its new grant."""
        email = "holder@example.com"
        session = await _session_for(email, ["deployer", "everyone"])
        await _cache_token_roles(redis, email)
        role, holder = _role_and_holder(email, capabilities=["run:apply"])
        db = FakeDB([role, holder])

        await roles_router.delete_role(role_name="deployer", user=_admin(), db=db)

        assert await get_session(session.token) is None
        assert not _token_roles_cached(redis, email)

    async def test_the_holders_are_read_before_the_delete(self, redis):
        """Reading them afterwards would find nobody, because the assignment rows
        go with the role. This fails if the lookup moves below `db.delete`."""
        email = "holder@example.com"
        session = await _session_for(email, ["deployer"])
        role, holder = _role_and_holder(email, capabilities=["run:apply"])

        class CascadingDB(FakeDB):
            async def delete(self, obj):
                await super().delete(obj)
                if isinstance(obj, Role):
                    self.rows = [r for r in self.rows if not isinstance(r, RoleAssignment)]

        await roles_router.delete_role(
            role_name="deployer", user=_admin(), db=CascadingDB([role, holder])
        )

        assert await get_session(session.token) is None


# ── the password reset ───────────────────────────────────────────────────


STRONG_PASSWORD = "quilted-lantern-vexes-9271"


def _user_patch(**attrs) -> users_router.UserUpdateRequest:
    return users_router.UserUpdateRequest(
        data=users_router.UserUpdateData(attributes=users_router.UserUpdateAttributes(**attrs))
    )


class TestAdminPasswordReset:
    async def test_the_users_sessions_end(self, redis):
        """A reset is how an admin takes an account back. Leaving whoever is
        already signed in signed in defeats the entire point."""
        email = "compromised@example.com"
        session = await _session_for(email, ["admin", "everyone"])
        await _cache_token_roles(redis, email)
        db = FakeDB([User(email=email, display_name=None, is_active=True, password_hash="old")])

        await users_router.update_user(
            email=email, body=_user_patch(password=STRONG_PASSWORD), user=_admin(), db=db
        )

        assert await get_session(session.token) is None
        assert not _token_roles_cached(redis, email)

    async def test_an_unrelated_edit_leaves_the_session_alone(self, redis):
        email = "renamed@example.com"
        session = await _session_for(email, ["everyone"])
        db = FakeDB([User(email=email, display_name=None, is_active=True, password_hash="old")])

        await users_router.update_user(
            email=email,
            body=_user_patch(**{"display-name": "New Name"}),
            user=_admin(),
            db=db,
        )

        assert await get_session(session.token) is not None


class TestTheTokenRolesPrefixIsNotRespelled:
    def test_the_propagation_module_shares_the_dependencies_constant(self) -> None:
        """Four hand-written copies of a cache key is a cache that silently stops
        being invalidated. This is the import, asserted."""
        assert role_change_propagation._TOKEN_ROLES_PREFIX is _TOKEN_ROLES_PREFIX

    def test_no_router_spells_the_prefix_by_hand(self) -> None:
        """A string CONSTANT, not a mention.

        Docstrings naming the key are documentation and welcome; a literal in an
        expression is a fourth copy nobody will remember to change. The
        distinction has to be made structurally — a substring search over the
        source flags the docstring in `users._revoke_all_user_access`, which is
        exactly the thing that should stay.
        """
        import ast
        import inspect

        for module in (ra_router, roles_router, users_router):
            tree = ast.parse(inspect.getsource(module))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(
                    node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
                )
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            offenders = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "tp:token_roles" in node.value
                and id(node) not in docstrings
            ]
            assert offenders == [], (
                f"{module.__name__} spells the token-role cache key literally "
                f"({offenders!r}). Use role_change_propagation.invalidate_token_roles "
                "so there is one place to change when the key moves."
            )


class TestTheDBFakeReallyReadsTheQuery:
    """If the fake ignored the WHERE clause, every test above would be asserting
    about rows the code never asked for."""

    async def test_it_filters_on_the_where_clause(self) -> None:
        wanted = RoleAssignment(provider_name="local", email="a@example.com", role_name="deployer")
        other = RoleAssignment(provider_name="local", email="b@example.com", role_name="auditor")
        db = FakeDB([wanted, other])

        result = await db.execute(
            select(RoleAssignment).where(RoleAssignment.role_name == "deployer")
        )

        assert result.scalars().all() == [wanted]

    async def test_it_shapes_a_column_select_as_tuples(self) -> None:
        db = FakeDB(
            [RoleAssignment(provider_name="okta", email="a@example.com", role_name="deployer")]
        )

        result = await db.execute(
            select(RoleAssignment.provider_name, RoleAssignment.email).where(
                RoleAssignment.role_name == "deployer"
            )
        )

        assert result.all() == [("okta", "a@example.com")]

    async def test_an_unreadable_where_clause_raises_rather_than_matching_everything(self) -> None:
        db = FakeDB([RoleAssignment(provider_name="local", email="a@e.com", role_name="x")])

        with pytest.raises(AssertionError):
            await db.execute(select(RoleAssignment).where(RoleAssignment.email.like("%@e.com")))
