"""A VCS connection may only be named by someone with a claim to it.

GHSA-v8g7-pqrj-8mcm. A connection covers every repository its credential reaches,
and its id is serialised to anyone with read on a workspace using it — so the id is
discoverable by design and naming one is a grant. These pin the three write
boundaries that accept a user-supplied connection id, and the one that does not.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services import vcs_connection_rbac as rbac


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
    the column defaulted true, the create path hardcoded false, and restore
    fell back to false — so two workspaces on one repo behaved differently
    depending on how they came to exist.
    """

    def test_the_column_defaults_permissive(self):
        from terrapod.db.models import AutodiscoveryRule, Workspace

        for model in (Workspace, AutodiscoveryRule):
            col = model.__table__.c["allow_fork_pr_plans"]
            assert col.default.arg is True, f"{model.__name__} ORM default"
            assert "true" in str(col.server_default.arg).lower(), f"{model.__name__} server_default"

    def test_the_create_path_agrees_with_the_column(self):
        """An explicit value in the INSERT overrides the ORM default, so the
        router's fallback has to say the same thing or the column default is
        unreachable for every workspace created through the API."""
        import inspect

        from terrapod.api.routers import tfe_v2

        src = inspect.getsource(tfe_v2)
        assert 'attrs.get("allow-fork-pr-plans", True)' in src, (
            "the create path does not fall back to this line's permissive default"
        )
        assert 'attrs.get("allow-fork-pr-plans", False)' not in src

    def test_restore_agrees_too(self):
        import inspect

        from terrapod.services import deleted_workspace_service

        src = inspect.getsource(deleted_workspace_service)
        assert 'settings.get("allow_fork_pr_plans", True)' in src, (
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
