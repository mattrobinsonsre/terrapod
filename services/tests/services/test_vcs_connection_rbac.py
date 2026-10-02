"""A VCS connection may only be named by someone with a claim to it.

GHSA-v8g7-pqrj-8mcm. A connection covers every repository its credential reaches,
and its id is serialised to anyone with read on a workspace using it — so the id is
discoverable by design and naming one is a grant. These pin the three write
boundaries that accept a user-supplied connection id, and the one that does not.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.db.models import VCSConnection
from terrapod.services import rbac_service as rbac_service_module
from terrapod.services import vcs_connection_rbac as rbac
from terrapod.services.vcs_connection_rbac import repository_allowed
from terrapod.services.vcs_provider import parse_repo_url


def _db(owns: bool, *, row_for: tuple[uuid.UUID, str] | None = None):
    """A db that answers the query it is actually given.

    The obvious fake ignores the statement and returns a row whenever `owns` is
    set, which makes every test below a test of the *caller* rather than of the
    filter: a predicate querying on the wrong columns, or binding the actor's
    email to the connection id, passes it unchanged. So this one compiles the
    statement and hands back a row only when the bound parameters genuinely
    carry the connection and the actor the caller claims to be asking about.

    `row_for` names the pair the stored workspace belongs to; it defaults to
    whatever the query asks for, which is the ordinary "this user does own such a
    workspace" case. Passing a different pair models the finding: a real row
    exists, but not one that answers *this* question.
    """
    db = AsyncMock()

    # `may_reference_connection` loads the connection by primary key first, so the
    # fake has to answer that too. A connection with no owner and no labels is the
    # shape every row has straight after the migration, which is exactly the case
    # these tests are about: the ownership fallback is the only claim available.
    conn = MagicMock()
    conn.name = "a-connection"
    conn.owner_email = ""
    conn.labels = {}
    conn.allowed_repositories = []
    db.get = AsyncMock(return_value=conn)

    async def execute(stmt, *a, **kw):
        params = set(stmt.compile().params.values())
        result = MagicMock()
        found = owns and (row_for is None or set(row_for) <= params)
        result.first.return_value = (uuid.uuid4(),) if found else None
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


class TestTheRule:
    async def test_a_platform_admin_may_name_any_connection(self):
        db = _db(owns=False)
        assert await rbac.may_reference_connection(
            db, conn_id=uuid.uuid4(), actor_email="a@x", is_platform_admin=True
        )
        # Not even asked — an admin needs no workspace to own.
        db.execute.assert_not_awaited()

    async def test_an_owner_of_a_workspace_using_it_may_name_it(self):
        assert await rbac.may_reference_connection(
            _db(owns=True), conn_id=uuid.uuid4(), actor_email="a@x", is_platform_admin=False
        )

    async def test_a_stranger_may_not(self):
        """The finding itself: an authenticated user with no claim to the connection."""
        assert not await rbac.may_reference_connection(
            _db(owns=False), conn_id=uuid.uuid4(), actor_email="a@x", is_platform_admin=False
        )

    async def test_an_empty_actor_is_refused_rather_than_matched(self):
        """`owner_email` defaults to empty on some rows, and `'' == ''` would match."""
        db = _db(owns=True)
        assert not await rbac.may_reference_connection(
            db, conn_id=uuid.uuid4(), actor_email="", is_platform_admin=False
        )
        db.execute.assert_not_awaited()

    async def test_the_switch_restores_the_previous_behaviour(self, monkeypatch):
        monkeypatch.setattr(
            rbac.settings.vcs, "require_connection_authorization", False, raising=False
        )
        assert await rbac.may_reference_connection(
            _db(owns=False), conn_id=uuid.uuid4(), actor_email="a@x", is_platform_admin=False
        )

    async def test_it_is_on_by_default(self):
        """The default IS the fix; a test that only toggled it would not notice."""
        from terrapod.config import settings

        assert settings.vcs.require_connection_authorization is True

    async def test_the_query_filters_on_BOTH_the_connection_and_the_owner(self):
        """Either filter alone grants far too much.

        On the connection alone, anyone owning any workspace passes. On the owner
        alone, owning one workspace would authorise every connection in the
        deployment — which is the finding, not a fix for it.
        """
        db = _db(owns=True)
        await rbac.may_reference_connection(
            db, conn_id=uuid.uuid4(), actor_email="a@x", is_platform_admin=False
        )
        sql = str(db.execute.await_args.args[0]).lower()
        assert "vcs_connection_id" in sql, sql
        assert "owner_email" in sql, sql

    async def test_a_row_belonging_to_someone_else_does_not_answer_this_question(self):
        """The substring check above sees the columns, not the values bound to them.

        Here the deployment really does hold a workspace owned by someone, using
        some connection — just not this pair. A predicate that queried on the
        connection and compared the owner against the wrong thing (or omitted the
        bind entirely) would read that row as a grant, which is the escalation.
        """
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        db = _db(owns=True, row_for=(theirs, "someone-else@x"))
        assert not await rbac.may_reference_connection(
            db, conn_id=mine, actor_email="a@x", is_platform_admin=False
        )

    async def test_and_the_matching_row_still_grants(self):
        """Otherwise the test above would pass against a predicate that refuses
        everyone, which is not the property either."""
        mine = uuid.uuid4()
        db = _db(owns=True, row_for=(mine, "a@x"))
        assert await rbac.may_reference_connection(
            db, conn_id=mine, actor_email="a@x", is_platform_admin=False
        )


class TestTheRefusalIsUseful:
    def test_it_names_the_connection_the_reason_and_the_way_out(self):
        cid = uuid.uuid4()
        msg = rbac.refusal_detail(cid)
        assert f"vcs-{cid}" in msg
        assert "every repository" in msg
        assert "require_connection_authorization" in msg


class TestTheMintIsGatedToo:
    """The run-time half. A variable value names the connection, so the
    create/PATCH gate cannot see it — nothing came through a workspace field.
    """

    @pytest.fixture
    def ws(self):
        w = MagicMock()
        w.vcs_connection_id = uuid.uuid4()
        w.owner_email = "owner@x"
        return w

    async def test_the_workspaces_own_connection_is_always_allowed(self, ws, monkeypatch):
        from terrapod.services import git_auth_service as g

        monkeypatch.setattr(g, "VCSConnection", MagicMock())
        conn = MagicMock(provider="github", token="t")
        db = AsyncMock()
        db.get.return_value = conn
        called = {"n": 0}

        async def _never(*a, **k):
            called["n"] += 1
            return False

        monkeypatch.setattr(
            "terrapod.services.vcs_connection_rbac.may_reference_connection", _never
        )
        monkeypatch.setattr(
            g, "_github_credential", AsyncMock(return_value={"username": "x"}), raising=False
        )
        # Its own connection: the authorization query must not even be reached.
        try:
            await g._mint_from_connection(
                db, f"vcs-{ws.vcs_connection_id}", "none", key="k", workspace=ws
            )
        except Exception:
            pass
        assert called["n"] == 0, "the workspace's own connection was put through the check"

    async def test_another_connection_is_refused_not_dropped(self, ws, monkeypatch):
        """Refused, so the run errors with the reason — a dropped credential would
        fail `init` naming neither the credential nor the cause."""
        from terrapod.services import git_auth_service as g

        db = AsyncMock()
        db.get.return_value = MagicMock(provider="github", token="t")

        async def _no(*a, **k):
            return False

        monkeypatch.setattr("terrapod.services.vcs_connection_rbac.may_reference_connection", _no)
        other = uuid.uuid4()
        with pytest.raises(g.GitAuthRefused) as e:
            await g._mint_from_connection(db, f"vcs-{other}", "none", key="mykey", workspace=ws)
        msg = str(e.value)
        assert f"vcs-{other}" in msg
        assert "mykey" in msg

    async def test_no_workspace_means_no_gate(self, monkeypatch):
        """Callers without a workspace (there are none in production, but the
        parameter is optional) must not be silently refused."""
        from terrapod.services import git_auth_service as g

        db = AsyncMock()
        db.get.return_value = MagicMock(provider="github", token="t")
        seen = {"n": 0}

        async def _count(*a, **k):
            seen["n"] += 1
            return False

        monkeypatch.setattr(
            "terrapod.services.vcs_connection_rbac.may_reference_connection", _count
        )
        try:
            await g._mint_from_connection(db, f"vcs-{uuid.uuid4()}", "none", key="k")
        except g.GitAuthRefused:
            pytest.fail("refused with no workspace to authorise against")
        except Exception:
            pass
        assert seen["n"] == 0


# ── The gates the review found unguarded ────────────────────────────────
#
# Each of these pins something that was shipped working and could have been
# removed with the whole suite still green. One of them — the `workspace=`
# kwarg — WAS missing on the 1.7 line, and nothing failed.


class TestTheRunTimeGateIsActuallyWired:
    """`resolve_git_auth(workspace=...)` is optional, so omitting it disables the
    run-time half of GHSA-v8g7 silently.

    That is not hypothetical: the 1.7 branch shipped the gate and omitted the
    kwarg, so `workspace` was None on every request and the authorization check
    was never reached. The mint's own tests call `_mint_from_connection` directly
    with a workspace, so they cannot see it. This drives the real caller.
    """

    async def test_next_run_passes_the_workspace_to_the_resolver(self):
        import inspect

        from terrapod.api.routers import runs as runs_router

        src = inspect.getsource(runs_router)
        assert "resolve_git_auth(db, resolved, workspace=" in src, (
            "next_run calls resolve_git_auth without workspace=, so the "
            "GHSA-v8g7 run-time gate short-circuits on every request"
        )
        assert "resolve_git_auth(db, resolved)" not in src, (
            "a call without the workspace kwarg remains"
        )
        # `workspace=None` satisfies the substring above and disables the gate just
        # as completely as omitting the kwarg — the review found this guard passed
        # against exactly that mutation.
        assert "workspace=None" not in src, (
            "the workspace is passed as None, so `if workspace is not None` "
            "short-circuits and the run-time gate never runs"
        )


class TestTheRegistryModulePathIsGated:
    """A module names a connection and a repo URL; the poller then clones with
    that connection's credential. Module creation is open to any authenticated
    user, so this path needs the same authorization as a workspace.
    """

    async def test_all_three_connection_sites_authorize(self):
        import inspect

        from terrapod.api.routers import registry_modules

        src = inspect.getsource(registry_modules)
        existence = src.count('detail="VCS connection not found"')
        gated = src.count("may_reference_connection(")
        assert existence > 0
        assert gated >= existence, (
            f"{existence} sites accept a connection id but only {gated} authorize "
            "it — an ungated site lets any authenticated user clone a private "
            "repository with someone else's installation credential"
        )


class TestTheForkGateDefaultOnThisLine:
    """The default is the decision, and it was split three ways once already:
    the column defaulted one way, the create path the other, and restore a third —
    so two workspaces on the same repository behaved differently depending on how
    they came to exist.

    1.8 shipped it permissive deliberately: a patch must not stop a fork pull
    request that plans today. This line takes the secure default, so the column,
    the create path and the autodiscovery template move together. Leaving the
    column permissive would re-open the setting for every workspace a rule
    creates, which reads exactly like the setting not working.
    """

    def test_the_column_defaults_closed(self):
        from terrapod.db.models import AutodiscoveryRule, Workspace

        for model in (Workspace, AutodiscoveryRule):
            col = model.__table__.c["allow_fork_pr_plans"]
            assert col.default.arg is False, f"{model.__name__} ORM default"
            assert "false" in str(col.server_default.arg).lower(), (
                f"{model.__name__} server_default"
            )

    def test_the_create_path_agrees_with_the_column(self):
        """An explicit value in the INSERT overrides the ORM default, so the
        router's fallback has to say the same thing or the column default is
        unreachable for every workspace created through the API."""
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2)
        assert 'attrs.get("allow-fork-pr-plans", False)' in src, (
            "the create path does not fall back to this line's closed default"
        )
        assert 'attrs.get("allow-fork-pr-plans", True)' not in src, (
            "the permissive 1.8 fallback is still here, so a workspace created "
            "through the API opts itself in whatever the column says"
        )

    def test_restore_agrees_too(self):
        import inspect

        from terrapod.services import deleted_workspace_service

        src = inspect.getsource(deleted_workspace_service)
        assert 'settings.get("allow_fork_pr_plans", False)' in src, (
            "restoring a workspace snapshotted before the column existed would "
            "silently differ from its never-deleted neighbours"
        )


class TestThePatchGateIsGuarded:
    """The create gate has route tests; the PATCH gate had none, and it carries
    its own change-detection logic (`!= _conn_before_patch`) that create does
    not. Deleting the PATCH block left the suite green."""

    async def test_patch_authorizes_a_changed_connection(self):
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2)
        assert "_conn_before_patch" in src, "the PATCH change-detection is gone"
        # the gate must sit after the value is applied and compare against the
        # captured pre-PATCH value, or an unchanged PATCH starts failing
        assert "ws.vcs_connection_id != _conn_before_patch" in src, (
            "the PATCH gate no longer fires only on a change"
        )
        assert src.count("may_reference_connection(") >= 2, "create and PATCH must both authorize"


class TestTheNarrowedTokenIsNarrowedAtEveryCallSite:
    """The narrowing (`8prq`) lands on two callers by virtue of the DEFAULT.

    So the guard was one level removed from the thing protected: the default has a
    test, but nothing stopped either call site passing
    `permissions=ALL_APP_PERMISSIONS` and handing a runner Job a token carrying
    every permission the App holds across every repository in the installation.
    A mutation review did exactly that and the whole suite stayed green.

    These are the only two places a minted token LEAVES the API process — the
    runner's git credential helper and the sparse-fetch Basic header — which is
    what makes them worth pinning individually rather than trusting the default.
    """

    CALLERS = (
        ("terrapod/services/git_auth_service.py", "the runner's git credential helper"),
        ("terrapod/services/git_fetch.py", "the sparse VCS fetch's Basic header"),
    )

    def test_neither_caller_asks_for_a_wider_token(self):
        import pathlib as _p
        import re

        root = _p.Path(__file__).resolve().parents[2]
        offenders = []
        for rel, why in self.CALLERS:
            src = (root / rel).read_text()
            for m in re.finditer(r"get_installation_token\(([^)]*)\)", src, re.S):
                args = m.group(1)
                if "permissions" in args and "CLONE_PERMISSIONS" not in args:
                    offenders.append(f"{rel}: {args.strip()[:80]}  ({why})")
        assert not offenders, (
            "a token that leaves the API process is minted with an explicit wider "
            "permission set, so the narrowing is bypassed at the one place it "
            f"matters:\n  {offenders}"
        )

    def test_both_callers_still_exist_where_this_test_thinks_they_are(self):
        """Otherwise the test above passes by reading nothing."""
        import pathlib as _p

        root = _p.Path(__file__).resolve().parents[2]
        for rel, _why in self.CALLERS:
            src = (root / rel).read_text()
            assert "get_installation_token(" in src, (
                f"{rel} no longer mints an installation token — if the call moved, "
                "this guard is pointed at the wrong file"
            )


class TestTheOwnerAndLabelClaims:
    """The 1.9.0 half (`option b`). v1.8.2 could only ask "do you already own a
    workspace on it", which worked and forced the FIRST workspace on any connection
    to be created by an admin. These are the two claims that remove that.
    """

    def _conn(self, *, owner="", labels=None, allowed=None):
        c = MagicMock()
        c.id = uuid.uuid4()
        c.name = "prod-github"
        c.owner_email = owner
        c.labels = labels or {}
        c.allowed_repositories = allowed or []
        return c

    def _db_with(self, conn, *, owns_workspace=False):
        """`db.get` answers the primary-key load of the connection; `db.execute`
        answers the workspace-ownership probe.

        Split the way the code splits, so a change from one to the other shows up
        here rather than silently falling through to "no claim".
        """
        db = AsyncMock()
        db.get = AsyncMock(return_value=conn)

        async def execute(stmt, *a, **kw):
            result = MagicMock()
            result.first.return_value = (uuid.uuid4(),) if owns_workspace else None
            return result

        db.execute = AsyncMock(side_effect=execute)
        return db

    async def test_the_connections_owner_may_name_it(self):
        conn = self._conn(owner="owner@x")
        assert await rbac.may_reference_connection(
            self._db_with(conn),
            conn_id=conn.id,
            actor_email="owner@x",
            is_platform_admin=False,
        )

    async def test_an_empty_owner_does_not_match_an_empty_actor(self):
        """`owner_email` defaults to empty on every row the migration adds, so
        `'' == ''` would hand every pre-existing connection to any caller."""
        conn = self._conn(owner="")
        assert not await rbac.may_reference_connection(
            self._db_with(conn), conn_id=conn.id, actor_email="", is_platform_admin=False
        )

    async def test_a_role_reaching_the_label_may_name_it(self, monkeypatch):
        conn = self._conn(labels={"team": "platform"})
        seen = {}

        async def fake_check(db, email, name, labels, roles):
            seen.update(email=email, name=name, labels=labels, roles=roles)
            return True

        monkeypatch.setattr(rbac_service_module, "check_access", fake_check)
        assert await rbac.may_reference_connection(
            self._db_with(conn),
            conn_id=conn.id,
            actor_email="a@x",
            is_platform_admin=False,
            actor_roles=["platform-team"],
        )
        # It must be asked about the CONNECTION's labels, not the workspace's.
        assert seen["labels"] == {"team": "platform"}
        assert seen["roles"] == ["platform-team"]

    async def test_no_roles_means_no_label_claim_rather_than_every_label(self):
        """The caller with no live principal — the run-time credential mint — passes
        no roles. If a missing argument widened access, the fix would reintroduce
        the finding it closes.
        """
        conn = self._conn(labels={"team": "platform"})
        assert not await rbac.may_reference_connection(
            self._db_with(conn), conn_id=conn.id, actor_email="a@x", is_platform_admin=False
        )

    async def test_the_workspace_ownership_path_still_grants(self):
        """Kept from v1.8.2 deliberately: removing it would break every deployment
        that upgraded onto it."""
        conn = self._conn()
        assert await rbac.may_reference_connection(
            self._db_with(conn, owns_workspace=True),
            conn_id=conn.id,
            actor_email="a@x",
            is_platform_admin=False,
        )

    async def test_a_connection_that_does_not_exist_is_not_a_claim(self):
        assert not await rbac.may_reference_connection(
            self._db_with(None), conn_id=uuid.uuid4(), actor_email="a@x", is_platform_admin=False
        )


class TestTheRepositoryAllowlist:
    """The residual hole after any amount of per-connection RBAC: being entitled to
    the connection says nothing about which repository it may be pointed at.

    Every case goes through a connection with a real `provider`, because the matcher
    canonicalises the URL with **the provider's own parser** — the same one the clone
    uses. The first version parsed the URL itself and the two disagreed, which voided
    the allowlist entirely: `myorg/safe?x=a://host/evilorg/evil` matched the pattern
    `myorg/safe` while the fetch resolved `evilorg/evil`. A fixture without a
    provider would exercise none of that.
    """

    def _conn(self, allowed, provider="github"):
        c = MagicMock()
        c.allowed_repositories = allowed
        c.provider = provider
        c.server_url = ""
        return c

    def test_empty_means_any_so_an_upgrade_changes_nothing(self):
        assert rbac.repository_allowed(self._conn([]), "https://github.com/anyone/anything")

    def test_a_pattern_matches_the_canonical_owner_slash_name(self):
        """An operator writes `myorg/*`, not a URL with a `.git` suffix. The
        canonical form comes from the parser, so one pattern covers every spelling of
        the same repository."""
        c = self._conn(["myorg/*"])
        for url in (
            "https://github.com/myorg/service",
            "https://github.com/myorg/service.git",
            "git@github.com:myorg/service.git",
        ):
            assert rbac.repository_allowed(c, url), url
        assert not rbac.repository_allowed(c, "https://github.com/other/service")

    def test_a_pattern_may_also_be_written_against_the_full_url(self):
        c = self._conn(["https://github.com/myorg/*"])
        assert rbac.repository_allowed(c, "https://github.com/myorg/service")
        assert not rbac.repository_allowed(c, "https://github.com/myorg2/service")

    def test_a_bare_owner_pattern_means_the_whole_owner(self):
        c = self._conn(["myorg"])
        assert rbac.repository_allowed(c, "https://github.com/myorg/anything")
        assert not rbac.repository_allowed(c, "https://github.com/other/anything")

    def test_the_parser_disagreement_bypass_is_closed(self):
        """THE finding in this function. A query string or fragment carrying a second
        `://` made the two parsers resolve different repositories, so the gate passed
        and the clone went elsewhere."""
        c = self._conn(["myorg/safe"])
        for url in (
            "myorg/safe?x=a://host/evilorg/evil",
            "myorg/safe#a://host/evilorg/evil",
        ):
            assert not rbac.repository_allowed(c, url), url

    def test_a_narrowed_connection_refuses_a_target_that_does_not_parse(self):
        """Failing closed costs nothing — the fetch would fail anyway — and failing
        open would let an unresolvable URL slip past a restriction."""
        assert not rbac.repository_allowed(self._conn(["myorg/*"]), "")
        assert not rbac.repository_allowed(self._conn(["myorg/*"]), "not a url at all")

    def test_a_blank_pattern_does_not_match_everything(self):
        """A stray empty string would otherwise become `fnmatch(x, "")` while reading
        as a configured allowlist."""
        assert not rbac.repository_allowed(self._conn([""]), "https://github.com/a/b")
        assert not rbac.repository_allowed(self._conn(["   "]), "https://github.com/a/b")

    def test_no_connection_is_not_permission(self):
        assert not rbac.repository_allowed(None, "https://github.com/a/b")

    def test_a_gitlab_group_pattern_reaches_nested_subgroups(self):
        """Recorded because it is wider than the pattern reads. GitLab's parser keeps
        the nested group path, so `group/*` matches at any depth — which is what a
        group-wide pattern is almost certainly meant to do, but is worth pinning so
        nobody discovers it as a surprise."""
        c = self._conn(["group/*"], provider="gitlab")
        assert rbac.repository_allowed(c, "https://gitlab.com/group/proj")
        assert rbac.repository_allowed(c, "https://gitlab.com/group/sub/proj")
        assert not rbac.repository_allowed(c, "https://gitlab.com/other/proj")


class TestEverySinkHasBehaviouralCoverage:
    """This class used to assert the sinks by reading their source, and those
    assertions survived the deletion of what they guarded.

    `"repository_allowed(" in src` is satisfied by any change that keeps the call and
    discards its verdict — proven by mutating the refs guard to
    `if not repository_allowed(...) and False:`, which left the endpoint an oracle for
    every repository the credential can reach and passed all three tests. And
    `src.count("_enforce_repository_allowlist(") >= 2` is satisfied by the `def` plus
    ONE call site, so deleting either the create or the PATCH enforcement passed too.

    The four sinks are now covered behaviourally in
    `tests/integration/test_workspace_vcs_allowlist.py` (refs endpoint, config fetch,
    workspace create, workspace PATCH), in
    `tests/integration/test_registry_module_vcs_allowlist.py` (the three registry
    paths), and in this file's `TestTheCredentialIsRefusedAtTheFetchItself` for the two
    clone-time guards. Each of those fails under the mutation above.

    What remains here is the one thing a behavioural test cannot do: fail when a NEW
    sink appears without a guard, since no test can drive a route that does not exist
    yet.
    """

    def test_every_path_that_accepts_a_repo_url_consults_the_allowlist(self):
        import inspect

        from terrapod.api.routers import registry_modules, tfe_v2, workspace_extensions
        from terrapod.services import vcs_config_service

        #: A module that reads a caller-supplied repository URL must reach the
        #: allowlist, by either name. Listed explicitly so that adding a module to the
        #: set is a deliberate act with a reviewer attached.
        must_check = {
            "tfe_v2": tfe_v2,
            "workspace_extensions": workspace_extensions,
            "registry_modules": registry_modules,
            "vcs_config_service": vcs_config_service,
        }
        missing = [
            name
            for name, mod in must_check.items()
            if not any(
                token in inspect.getsource(mod)
                for token in ("repository_allowed(", "_enforce_repository_allowlist(")
            )
        ]
        assert not missing, (
            "these accept a repository URL without reaching the allowlist at all:\n  "
            + "\n  ".join(missing)
            + "\n\nThis is a presence check and deliberately weak — it cannot see a "
            "call whose verdict is discarded. The real coverage is behavioural; add a "
            "route-driven test alongside any new sink."
        )

    def test_the_clone_itself_is_guarded_in_both_fetch_functions(self):
        """The layer the accepting paths cannot provide: a URL stored before a
        narrowing must stop being cloned, and the poller clones before a run checks
        anything."""
        import inspect

        from terrapod.services import vcs_archive_cache, vcs_provider

        for mod in (vcs_provider, vcs_archive_cache):
            assert "repository_pair_allowed(" in inspect.getsource(mod), (
                f"{mod.__name__} fetches a repository without the allowlist, so a "
                "narrowing does not reach the clone"
            )


class TestTheAllowlistCannotBeBypassedByCraftingTheUrl:
    """The matcher compares against the repository the CLONE will use, and nothing else.

    This has been got wrong twice, in two different ways, and both times the
    allowlist was wholly defeated rather than merely loosened:

    1. Matching `urlparse(url).path`, which drops the query string, while
       `parse_repo_url` splits on the first `://` anywhere in the string.
    2. Matching the canonical form correctly and ALSO offering the raw URL as a
       spelling a pattern could match. `fnmatch`'s `*` crosses `/`, so the ordinary
       pattern `myorg/*` matched the whole of
       `myorg/safe?x=a://github.com/othercorp/private` while the fetch cloned
       `othercorp/private`.

    So these assert the PROPERTY rather than either implementation: for every
    spelling, the verdict must agree with what `parse_repo_url` resolves. A test that
    only pinned "the crafted string is refused" would pass against a third wrong
    matcher that happened to refuse that one input.
    """

    @staticmethod
    def _conn(allowed):
        c = SimpleNamespace()
        c.provider = "github"
        c.server_url = "https://api.github.com"
        c.allowed_repositories = allowed
        return c

    @pytest.mark.parametrize(
        "url",
        [
            "myorg/safe?x=a://github.com/othercorp/private",
            "myorg/anything://github.com/othercorp/private",
            "myorg/x#y://github.com/othercorp/private",
            "myorg/safe/../../othercorp/private://github.com/othercorp/private",
        ],
    )
    def test_a_url_that_resolves_elsewhere_is_refused_however_it_is_spelled(self, url):
        conn = self._conn(["myorg/*"])
        resolved = parse_repo_url(conn, url)
        assert resolved == ("othercorp", "private"), (
            "the fixture no longer resolves out of scope, so it proves nothing"
        )
        assert repository_allowed(conn, url) is False, (
            f"{url!r} was allowed by the pattern 'myorg/*' while the clone would "
            f"fetch {resolved[0]}/{resolved[1]} — the allowlist is bypassable"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "myorg/safe?x=a://github.com/othercorp/private",
            "myorg/anything://github.com/othercorp/private",
        ],
    )
    def test_nor_by_a_pattern_written_against_a_full_address(self, url):
        """The crafted URL must not satisfy a full-address pattern either.

        Matching the raw URL only when the pattern contains `://` looks like it
        would close the hole and does not: `*` still crosses everything after the
        host, so `https://github.com/myorg/*` would match the crafted string too.
        """
        conn = self._conn(["https://github.com/myorg/*"])
        assert repository_allowed(conn, url) is False

    def test_a_full_address_pattern_still_matches_what_it_should(self):
        """The convenience the raw form existed for, kept by reducing the PATTERN."""
        conn = self._conn(["https://github.com/myorg/*"])
        assert repository_allowed(conn, "https://github.com/myorg/safe") is True
        assert repository_allowed(conn, "https://github.com/othercorp/private") is False

    def test_a_nested_group_pattern_written_as_an_address_still_matches(self):
        conn = SimpleNamespace()
        conn.provider = "gitlab"
        conn.server_url = "https://gitlab.example.com"
        conn.allowed_repositories = ["https://gitlab.example.com/group/sub/*"]
        assert repository_allowed(conn, "https://gitlab.example.com/group/sub/proj") is True
        assert repository_allowed(conn, "https://gitlab.example.com/other/proj") is False

    def test_patterns_are_case_sensitive_on_every_platform(self):
        """`fnmatch` case-folds via `os.path.normcase`, so it is case-INsensitive on
        a macOS dev box and case-sensitive in production — a verdict that differs
        between where a pattern is written and where it is enforced. The docs promise
        case-sensitive, so the matcher uses `fnmatchcase`.
        """
        assert repository_allowed(self._conn(["MyOrg/*"]), "https://github.com/myorg/safe") is False
        assert repository_allowed(self._conn(["myorg/*"]), "https://github.com/myorg/safe") is True

    def test_a_pattern_naming_no_repository_does_not_widen_the_connection(self):
        """`https://host/` reduces to the empty string. Matching everything with it
        would turn a malformed entry into an allow-all, which is the direction a
        narrowed connection must never fail in.
        """
        conn = self._conn(["https://github.com/"])
        assert repository_allowed(conn, "https://github.com/othercorp/private") is False


class TestTheCredentialIsRefusedAtTheFetchItself:
    """GHSA-v8g7-pqrj-8mcm. Checking only the paths that ACCEPT a repository URL
    leaves two holes, and the documentation described a narrower residual gap than
    existed:

    1. A URL set while a connection was wide keeps being cloned after a narrowing.
    2. The workspace poller clones *before* anything a run would check, so by the time
       the config fetch refuses the run, the credential has already read the
       out-of-scope repository — which is the thing the control exists to prevent.

    So the allowlist is enforced at the two places the credential is actually used as
    well. These take `(conn, owner, repo)`, so there is no second string to derive —
    which is what broke this control twice before.
    """

    async def test_the_provider_dispatcher_refuses_an_out_of_scope_repo(self):
        from terrapod.services.vcs_connection_rbac import RepositoryNotAllowed
        from terrapod.services.vcs_provider import download_archive

        conn = VCSConnection(id=uuid.uuid4(), provider="github", allowed_repositories=["myorg/*"])
        with pytest.raises(RepositoryNotAllowed) as exc:
            await download_archive(conn, "otherorg", "private", "main")
        assert "otherorg/private" in str(exc.value)

    async def test_the_provider_dispatcher_allows_one_in_scope(self):
        """Reaches the real provider call, which fails for want of credentials — any
        error that is NOT the refusal proves the guard let it through."""
        from terrapod.services.vcs_connection_rbac import RepositoryNotAllowed
        from terrapod.services.vcs_provider import download_archive

        conn = VCSConnection(id=uuid.uuid4(), provider="github", allowed_repositories=["myorg/*"])
        with pytest.raises(Exception) as exc:
            await download_archive(conn, "myorg", "thing", "main")
        assert not isinstance(exc.value, RepositoryNotAllowed)

    async def test_the_archive_cache_refuses_an_out_of_scope_repo(self):
        from terrapod.services.vcs_archive_cache import VCSArchiveCache
        from terrapod.services.vcs_connection_rbac import RepositoryNotAllowed

        conn = VCSConnection(id=uuid.uuid4(), provider="github", allowed_repositories=["myorg/*"])
        with pytest.raises(RepositoryNotAllowed) as exc:
            await VCSArchiveCache().get_or_fetch(conn, "otherorg", "private", "abc123")
        assert "otherorg/private" in str(exc.value)

    async def test_an_empty_allowlist_leaves_both_fetch_paths_alone(self):
        """Every deployment that has not opted in, which must be unaffected."""
        from terrapod.services.vcs_archive_cache import VCSArchiveCache
        from terrapod.services.vcs_connection_rbac import RepositoryNotAllowed
        from terrapod.services.vcs_provider import download_archive

        conn = VCSConnection(id=uuid.uuid4(), provider="github", allowed_repositories=[])
        for call in (
            download_archive(conn, "anyorg", "anyrepo", "main"),
            VCSArchiveCache().get_or_fetch(conn, "anyorg", "anyrepo", "abc123"),
        ):
            with pytest.raises(Exception) as exc:
                await call
            assert not isinstance(exc.value, RepositoryNotAllowed)

    def test_a_refusal_is_a_permission_error_not_a_transport_one(self):
        """So `except OSError` meant for the network cannot swallow it while looking
        like a flaky clone."""
        from terrapod.services.vcs_connection_rbac import RepositoryNotAllowed

        assert issubclass(RepositoryNotAllowed, PermissionError)

    def test_the_pair_entry_point_agrees_with_the_url_one(self):
        """The two must never diverge: a divergence is exactly the shape of both
        historical breaks of this control."""
        from terrapod.services.vcs_connection_rbac import (
            repository_allowed,
            repository_pair_allowed,
        )

        for pats in (["myorg/*"], ["myorg"], ["myorg/safe"], [], ["*"]):
            conn = VCSConnection(provider="github", allowed_repositories=pats)
            for owner, repo in (("myorg", "safe"), ("myorg", "other"), ("otherorg", "x")):
                assert repository_pair_allowed(conn, owner, repo) == repository_allowed(
                    conn, f"https://github.com/{owner}/{repo}"
                ), (pats, owner, repo)


class TestAPinnedTokenCannotEscapeItsPinThroughTheLabelPath:
    """`check_access` short-circuits to True on `admin`, so handing it the LIVE role
    set re-granted through the label path exactly the admin a pin had removed.

    The explicit gate above is correct — it tests `"admin" in
    effective_platform_roles(user)`, which attenuates. The label path then received
    `list(user.roles)`, which does not, so a `service_bound` token pinned away from
    `admin` and held by an admin was refused by the gate and granted by the label.

    `user.roles` is also wider than a `service_bound` pin for CUSTOM roles, so the
    escape was not limited to admin. `dependencies.label_reach_roles` intersects, and
    this gate drops `admin` again as defence in depth.
    """

    async def test_admin_in_the_role_list_does_not_grant_through_labels(self):
        conn = VCSConnection(id=uuid.uuid4(), owner_email="", labels={"team": "net"})
        db = AsyncMock()
        db.get = AsyncMock(return_value=conn)
        db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

        allowed = await rbac.may_reference_connection(
            db,
            conn_id=conn.id,
            actor_email="pinned@example.com",
            is_platform_admin=False,
            actor_roles=["admin"],
        )
        assert allowed is False, (
            "`admin` in the label-path role list re-grants what the pin removed"
        )

    def test_label_reach_roles_intersects_a_bound_pin(self):
        from terrapod.api.dependencies import label_reach_roles

        user = SimpleNamespace(
            roles=["admin", "net-team", "db-team"],
            kind="service_bound",
            pinned_roles=["net-team"],
        )
        assert label_reach_roles(user) == {"net-team"}

    def test_label_reach_roles_intersects_a_detached_pin_with_the_live_set(self):
        """Pinned-only would keep a role the principal has since lost."""
        from terrapod.api.dependencies import label_reach_roles

        user = SimpleNamespace(
            roles=["net-team"],
            kind="service_detached",
            pinned_roles=["net-team", "revoked-team"],
        )
        assert label_reach_roles(user) == {"net-team"}

    def test_label_reach_roles_leaves_an_interactive_principal_alone(self):
        from terrapod.api.dependencies import label_reach_roles

        user = SimpleNamespace(roles=["net-team"], kind="interactive", pinned_roles=None)
        assert label_reach_roles(user) == {"net-team"}

    def test_label_reach_roles_drops_admin_for_an_interactive_admin_too(self):
        """Costs a genuine admin nothing: `is_platform_admin` returns True long before
        the label path is reached."""
        from terrapod.api.dependencies import label_reach_roles

        user = SimpleNamespace(roles=["admin"], kind="interactive", pinned_roles=None)
        assert label_reach_roles(user) == set()

    def test_every_call_site_narrows_the_role_set(self):
        """A new caller passing `user.roles` would reopen this silently, and no
        behavioural test can see a call site that does not exist yet."""
        import pathlib
        import re

        root = pathlib.Path(rbac.__file__).resolve().parents[1]
        offenders = []
        for path in root.rglob("*.py"):
            src = path.read_text()
            if "may_reference_connection(" not in src:
                continue
            for m in re.finditer(r"actor_roles=([^,\n]+)", src):
                expr = m.group(1).strip()
                if "label_reach_roles" not in expr:
                    offenders.append(f"{path.name}: actor_roles={expr}")
        assert not offenders, (
            "a call site passes an un-narrowed role set to the label path:\n  "
            + "\n  ".join(offenders)
        )


class TestARefusalAtTheCloneCostsOneWorkspaceNotTheCycle:
    """The blast radius of enforcing the allowlist at the clone, asserted rather than
    assumed — this is the property that would make the change dangerous if wrong.

    The poller walks every workspace in one pass. If `RepositoryNotAllowed` escaped the
    per-workspace handler, narrowing ONE connection would stop the poll cycle for the
    whole deployment, and every workspace would quietly stop picking up commits. The
    handler that contains it is a broad pre-existing `except Exception`, so this test
    exists to stop someone narrowing it later to something that no longer catches a
    `PermissionError`.
    """

    def test_the_refusal_is_catchable_by_the_handlers_that_wrap_the_fetch(self):
        from terrapod.services.vcs_connection_rbac import RepositoryNotAllowed

        # `PermissionError` -> `OSError` -> `Exception`. The poller's handler is
        # `except Exception`, and an `except OSError` for transport would also catch it.
        assert issubclass(RepositoryNotAllowed, Exception)
        assert issubclass(RepositoryNotAllowed, OSError)

    def test_the_poller_wraps_both_fetch_call_sites(self):
        """A source check, because driving a full poll cycle to assert "the other
        workspaces still got polled" needs the whole VCS fixture — and the property is
        positional: the call must sit inside a `try`."""
        import inspect
        import re

        from terrapod.services import vcs_poller

        src = inspect.getsource(vcs_poller)
        unwrapped = []
        for m in re.finditer(r"\n([ \t]*)(?:\w+ = )?await (?:cache|meta)\.get_or_fetch\(", src):
            before = src[: m.start()].rstrip().splitlines()
            j = len(before) - 1
            while j >= 0 and (before[j].strip().startswith("#") or not before[j].strip()):
                j -= 1
            if j >= 0 and before[j].strip() != "try:":
                unwrapped.append(before[j].strip()[:70])
        assert not unwrapped, (
            "a get_or_fetch in the poller is not the first statement in a try:, so a "
            "repository refused by the allowlist would abort the poll cycle for every "
            "other workspace too:\n  " + "\n  ".join(unwrapped)
        )
