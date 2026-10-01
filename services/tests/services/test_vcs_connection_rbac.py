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


def _db(owns: bool):
    """A db whose ownership query finds a row, or does not."""
    db = AsyncMock()
    result = MagicMock()
    result.first.return_value = (uuid.uuid4(),) if owns else None
    db.execute.return_value = result
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


# `TestTheForkGateDefaultOnThisLine` is deliberately absent here: the fork-PR
# gate and its `allow_fork_pr_plans` column ship on 1.8 and above only, because
# the migration can be sequenced onto one release line. On 1.8 that class pins
# the default the column, the create path and restore must all agree on.


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
