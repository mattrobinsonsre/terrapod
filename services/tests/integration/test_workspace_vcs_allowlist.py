"""The repository allowlist is enforced at every sink, proved by driving each one.

GHSA-v8g7-pqrj-8mcm's second half. `repository_allowed` is called from four places:
workspace create and PATCH (`tfe_v2`), the `vcs-refs` endpoint
(`workspace_extensions`) and the config fetch (`vcs_config_service`). The registry
module door has its own file.

**These replace source-introspection assertions, which could not do the job.** The
call sites were pinned by `inspect.getsource(...)` substring checks — one of them
`src.count("_enforce_repository_allowlist(") >= 2`, which the function's own `def`
line contributes to, so deleting either call site left it green. A guard that
survives the deletion of the thing it guards is not a guard.

Integration tier, because the control is a sequence of real writes: the realistic
way a workspace comes to point outside its connection's scope is ORDER — the URL was
set while the connection was open, and an operator narrowed the connection
afterwards. That cannot be expressed against a mocked session without the fixture
deciding the answer.

Nothing here reaches a provider. The refusals happen before any network call, and
each positive contrast is arranged to fail just past the allowlist with a different,
recognisable status, so "we got past the gate" is asserted without a live GitLab.
"""

import uuid

import pytest
from sqlalchemy import select

from tests.integration.conftest import admin_user, set_auth

pytestmark = pytest.mark.integration

CONNS = "/api/terrapod/v1/vcs-connections"
WS = "/api/v2/organizations/default/workspaces"
CT = {"Content-Type": "application/vnd.api+json"}

IN_SCOPE = "https://gitlab.com/myorg/safe"
OUT_OF_SCOPE = "https://gitlab.com/othercorp/private"
UNPARSEABLE = "not-even-a-url"


async def _connection(client, allowed: list[str]) -> str:
    """Seeded through the real route, so the fixture cannot drift from the schema."""
    r = await client.post(
        CONNS,
        json={
            "data": {
                "type": "vcs-connections",
                "attributes": {
                    "name": f"c-{uuid.uuid4().hex[:8]}",
                    "provider": "gitlab",
                    "token": "glpat-fixture",
                    "allowed-repositories": allowed,
                },
            }
        },
        headers=CT,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["data"]["id"]


async def _narrow(client, conn_id: str, allowed: list[str]) -> None:
    r = await client.patch(
        f"{CONNS}/{conn_id}",
        json={"data": {"type": "vcs-connections", "attributes": {"allowed-repositories": allowed}}},
        headers=CT,
    )
    assert r.status_code == 200, r.text


async def _create_ws(client, name: str, conn_id: str | None = None, repo_url: str = ""):
    attrs: dict = {"name": name}
    if conn_id is not None:
        attrs["vcs-connection-id"] = conn_id
        attrs["vcs-repo-url"] = repo_url
    return await client.post(
        WS, json={"data": {"type": "workspaces", "attributes": attrs}}, headers=CT
    )


async def _patch_ws(client, ws_id: str, attrs: dict):
    return await client.patch(
        f"/api/v2/workspaces/{ws_id}",
        json={"data": {"type": "workspaces", "attributes": attrs}},
        headers=CT,
    )


async def _read_ws(client, ws_id: str) -> dict:
    r = await client.get(f"/api/v2/workspaces/{ws_id}")
    assert r.status_code == 200, r.text
    return r.json()["data"]["attributes"]


class TestWorkspaceCreate:
    async def test_an_out_of_scope_url_is_refused(self, app, client):
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])

        r = await _create_ws(client, f"ws-{uuid.uuid4().hex[:8]}", cid, OUT_OF_SCOPE)
        assert r.status_code == 403, r.text
        assert "othercorp/private" in r.text, "the refusal does not name the repository"

    async def test_an_in_scope_url_is_accepted(self, app, client):
        """A guard that refuses legitimate work gets switched off, so this carries
        as much weight as the refusal."""
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])

        r = await _create_ws(client, f"ws-{uuid.uuid4().hex[:8]}", cid, IN_SCOPE)
        assert r.status_code in (200, 201), r.text

    async def test_an_open_connection_still_accepts_anything(self, app, client):
        """The allowlist is opt-in: an empty list means any repository, which is what
        every deployment has until an operator narrows one."""
        set_auth(app, admin_user())
        cid = await _connection(client, [])

        r = await _create_ws(client, f"ws-{uuid.uuid4().hex[:8]}", cid, OUT_OF_SCOPE)
        assert r.status_code in (200, 201), r.text


class TestWorkspacePatch:
    async def test_the_url_cannot_be_repointed_out_of_scope(self, app, client):
        """The hole a URL-only PATCH leaves open. `vcs-repo-url` is separately
        settable and the connection does not change, so a check that fired only when
        the connection moved would let an entitled owner repoint a narrowed
        credential at anything it can read."""
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])
        name = f"ws-{uuid.uuid4().hex[:8]}"
        created = await _create_ws(client, name, cid, IN_SCOPE)
        assert created.status_code in (200, 201), created.text
        ws_id = created.json()["data"]["id"]

        r = await _patch_ws(client, ws_id, {"vcs-repo-url": OUT_OF_SCOPE})
        assert r.status_code == 403, (
            f"a narrowed connection was repointed by a URL-only PATCH: {r.text}"
        )
        assert (await _read_ws(client, ws_id))["vcs-repo-url"] == IN_SCOPE, (
            "the refusal did not roll back"
        )

    async def test_an_in_scope_move_still_works(self, app, client):
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])
        name = f"ws-{uuid.uuid4().hex[:8]}"
        created = await _create_ws(client, name, cid, IN_SCOPE)
        ws_id = created.json()["data"]["id"]

        r = await _patch_ws(client, ws_id, {"vcs-repo-url": "https://gitlab.com/myorg/other"})
        assert r.status_code == 200, r.text
        assert (await _read_ws(client, ws_id))["vcs-repo-url"] == "https://gitlab.com/myorg/other"

    async def test_attaching_a_narrowed_connection_to_an_out_of_scope_url(self, app, client):
        """Swapping the connection is the other half: the workspace's URL was
        already there, and the new connection is not scoped to it."""
        set_auth(app, admin_user())
        openc = await _connection(client, [])
        shut = await _connection(client, ["myorg/*"])
        created = await _create_ws(client, f"ws-{uuid.uuid4().hex[:8]}", openc, OUT_OF_SCOPE)
        assert created.status_code in (200, 201), created.text
        ws_id = created.json()["data"]["id"]

        r = await _patch_ws(client, ws_id, {"vcs-connection-id": shut})
        assert r.status_code == 403, r.text

    async def test_an_unrelated_patch_on_an_out_of_scope_workspace_is_also_refused(
        self, app, client
    ):
        """Deliberate: the check runs on every PATCH that leaves a connection
        attached, so a workspace that has drifted out of scope cannot be edited at
        all until its URL or the allowlist is fixed. Pinned so the behaviour is a
        decision rather than a surprise."""
        set_auth(app, admin_user())
        cid = await _connection(client, [])
        created = await _create_ws(client, f"ws-{uuid.uuid4().hex[:8]}", cid, OUT_OF_SCOPE)
        ws_id = created.json()["data"]["id"]
        await _narrow(client, cid, ["myorg/*"])

        r = await _patch_ws(client, ws_id, {"description": "unrelated"})
        assert r.status_code == 403, r.text


class TestTheVcsRefsEndpoint:
    """ "Which branches and tags does this repository have", answered at
    workspace-READ. That makes it a private-repository oracle for anything the
    connection's credential can reach, which is why the finding named it."""

    async def test_a_workspace_that_drifted_out_of_scope_is_refused(self, app, client):
        set_auth(app, admin_user())
        cid = await _connection(client, [])
        created = await _create_ws(client, f"ws-{uuid.uuid4().hex[:8]}", cid, OUT_OF_SCOPE)
        assert created.status_code in (200, 201), created.text
        ws_id = created.json()["data"]["id"]

        # the operator narrows the connection AFTERWARDS — the realistic order
        await _narrow(client, cid, ["myorg/*"])

        r = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/vcs-refs")
        assert r.status_code == 403, (
            f"the refs endpoint is still an oracle for an out-of-scope repository: {r.text}"
        )
        assert "othercorp/private" in r.text

    async def test_an_open_connection_gets_past_the_allowlist(self, app, client):
        """The contrast, without a live provider: an unparseable URL on an OPEN
        connection reaches the parse step and 422s there, which is only possible
        after the allowlist has allowed it. A narrowed connection refuses the same
        URL at 403, because a target nobody can resolve is not in any scope."""
        set_auth(app, admin_user())
        cid = await _connection(client, [])
        created = await _create_ws(client, f"ws-{uuid.uuid4().hex[:8]}", cid, UNPARSEABLE)
        ws_id = created.json()["data"]["id"]

        r = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/vcs-refs")
        assert r.status_code == 422, r.text
        assert "Cannot parse" in r.text

        await _narrow(client, cid, ["myorg/*"])
        r = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}/vcs-refs")
        assert r.status_code == 403, r.text


class TestTheConfigFetch:
    """Where the source actually arrives, and the sink with no live principal: the
    poller and a run trigger reach it with nobody to refuse. Failing here stops the
    clone rather than the request, so the run errors with a reason."""

    @staticmethod
    async def _fetch(name: str) -> None:
        """Drive the real fetch for the workspace called *name*, in its own session."""
        from terrapod.db.models import Workspace
        from terrapod.db.session import get_db_session
        from terrapod.services.vcs_config_service import fetch_config_version

        async with get_db_session() as db:
            ws = (
                await db.execute(select(Workspace).where(Workspace.name == name))
            ).scalar_one_or_none()
            assert ws is not None, "the fixture workspace was not written"
            await fetch_config_version(db, ws)

    async def test_an_out_of_scope_workspace_cannot_be_fetched_for(self, app, client):
        from terrapod.services.vcs_config_service import VCSConfigError

        set_auth(app, admin_user())
        cid = await _connection(client, [])
        name = f"ws-{uuid.uuid4().hex[:8]}"
        assert (await _create_ws(client, name, cid, OUT_OF_SCOPE)).status_code in (200, 201)
        await _narrow(client, cid, ["myorg/*"])

        with pytest.raises(VCSConfigError) as exc:
            await self._fetch(name)

        # Named, so the message distinguishes this refusal from the parse failure the
        # contrast below produces — both are VCSConfigError, so asserting the type
        # alone would pass against either.
        assert "restricted to specific repositories" in str(exc.value), str(exc.value)
        assert "othercorp/private" in str(exc.value)

    async def test_an_open_connection_gets_past_the_allowlist(self, app, client):
        from terrapod.services.vcs_config_service import VCSConfigError

        set_auth(app, admin_user())
        cid = await _connection(client, [])
        name = f"ws-{uuid.uuid4().hex[:8]}"
        assert (await _create_ws(client, name, cid, UNPARSEABLE)).status_code in (200, 201)

        with pytest.raises(VCSConfigError) as exc:
            await self._fetch(name)

        assert "Cannot parse" in str(exc.value), (
            f"the fetch refused before reaching the parse step: {exc.value}"
        )
