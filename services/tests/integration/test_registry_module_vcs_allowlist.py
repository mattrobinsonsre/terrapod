"""A registry module cannot be pointed outside its connection's allowlist.

GHSA-v8g7-pqrj-8mcm, the registry half. A module names a VCS connection and an
arbitrary `vcs_repo_url`; the registry poller then clones that repository with that
connection's credential. Module creation is open to any authenticated user, so the
allowlist has to bound this door as well as the workspace one.

Integration tier, not services-api, for two reasons: the behaviour is a sequence of
real writes (create in scope, then PATCH out of scope, where the second request must
read what the first stored), and the route's own structure was the defect — a mocked
session would have been shaped around whatever the handler happened to do.

**Every test here failed against the code as first written**, which is the point of
writing them at this tier:

- attaching a connection ran `module.vcs_connection_id = conn_id` INSIDE the
  `raise HTTPException` body, so it was unreachable and the attach silently did
  nothing;
- the `else` that detaches bound to the allowlist `if` rather than to
  `if vcs_conn_val:`, so the SUCCESS path detached the module and returned 200;
- detaching (`vcs-connection-id: null`) read names bound only in the other branch
  and raised `UnboundLocalError` -> 500;
- attaching without restating `vcs-repo-url` subscripted a key that was not there
  and raised `KeyError` -> 500;
- and a URL-only PATCH skipped the whole block, so a module created in scope could
  be repointed anywhere the credential reached.
"""

import uuid

import pytest

from tests.integration.conftest import admin_user, set_auth

pytestmark = pytest.mark.integration

MODULES = "/api/terrapod/v1/registry-modules"
CT = {"Content-Type": "application/vnd.api+json"}


def _mod_path(name: str, provider: str = "aws") -> str:
    return f"{MODULES}/private/default/{name}/{provider}"


async def _connection(client, allowed: list[str]) -> str:
    """Seeded through the real route, so the fixture cannot drift from the schema.

    Returns the prefixed id exactly as the API gives it — no reconstruction, which is
    how an id-format change would otherwise slip past a test like this.
    """
    r = await client.post(
        "/api/terrapod/v1/vcs-connections",
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


async def _read(client, name: str) -> dict:
    r = await client.get(_mod_path(name))
    assert r.status_code == 200, r.text
    return r.json()["data"]["attributes"]


async def _create(client, name: str, conn_id=None, repo_url=""):
    attrs: dict = {"name": name, "provider": "aws"}
    if conn_id is not None:
        attrs["vcs-connection-id"] = conn_id
        attrs["vcs-repo-url"] = repo_url
    return await client.post(
        MODULES,
        json={"data": {"type": "registry-modules", "attributes": attrs}},
        headers=CT,
    )


async def _patch(client, name: str, attrs: dict):
    return await client.patch(
        _mod_path(name),
        json={"data": {"type": "registry-modules", "attributes": attrs}},
        headers=CT,
    )


class TestAttachingAConnectionActuallyAttachesIt:
    async def test_an_in_scope_url_attaches_and_persists(self, app, client):
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])
        name = f"m-{uuid.uuid4().hex[:8]}"
        assert (await _create(client, name)).status_code in (200, 201)

        r = await _patch(
            client,
            name,
            {
                "vcs-connection-id": cid,
                "vcs-repo-url": "https://gitlab.com/myorg/thing",
            },
        )
        assert r.status_code == 200, r.text

        # Read it back from the DATABASE, not from the response: the defect returned
        # 200 while writing `vcs_connection_id = None`, so a response-shape assertion
        # would have passed over it.
        got = await _read(client, name)
        assert got["vcs-connection-id"] == cid, (
            "the connection was not attached — the success path detached it instead"
        )

    async def test_attaching_without_restating_the_url_is_not_a_500(self, app, client):
        """A partial update may attach a connection and leave the URL alone."""
        set_auth(app, admin_user())
        cid = await _connection(client, [])  # empty = any repository
        name = f"m-{uuid.uuid4().hex[:8]}"
        assert (await _create(client, name)).status_code in (200, 201)

        r = await _patch(client, name, {"vcs-connection-id": cid})
        assert r.status_code == 200, r.text

    async def test_detaching_is_not_a_500(self, app, client):
        set_auth(app, admin_user())
        cid = await _connection(client, [])
        name = f"m-{uuid.uuid4().hex[:8]}"
        assert (await _create(client, name, cid, "https://gitlab.com/myorg/thing")).status_code in (
            200,
            201,
        )

        r = await _patch(client, name, {"vcs-connection-id": None})
        assert r.status_code == 200, r.text
        assert (await _read(client, name))["vcs-connection-id"] is None


class TestTheAllowlistBoundsTheModule:
    async def test_attaching_with_an_out_of_scope_url_is_refused(self, app, client):
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])
        name = f"m-{uuid.uuid4().hex[:8]}"
        assert (await _create(client, name)).status_code in (200, 201)

        r = await _patch(
            client,
            name,
            {
                "vcs-connection-id": cid,
                "vcs-repo-url": "https://gitlab.com/othercorp/private",
            },
        )
        assert r.status_code == 403, r.text
        assert "othercorp/private" in r.text

    async def test_creating_out_of_scope_is_refused(self, app, client):
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])
        r = await _create(
            client,
            f"m-{uuid.uuid4().hex[:8]}",
            cid,
            "https://gitlab.com/othercorp/private",
        )
        assert r.status_code == 403, r.text

    async def test_the_url_cannot_be_repointed_out_of_scope_on_its_own(self, app, client):
        """The hole a URL-only PATCH left open.

        `vcs-repo-url` is separately settable, so a module created in scope could be
        repointed afterwards with no `vcs-connection-id` in the body — which skipped
        the entire connection block — and the poller would then clone the new target
        with the narrowed connection's credential.
        """
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])
        name = f"m-{uuid.uuid4().hex[:8]}"
        assert (await _create(client, name, cid, "https://gitlab.com/myorg/safe")).status_code in (
            200,
            201,
        )

        r = await _patch(client, name, {"vcs-repo-url": "https://gitlab.com/othercorp/private"})
        assert r.status_code == 403, (
            "a narrowed connection was repointed at an out-of-scope repository by a "
            f"URL-only PATCH: {r.text}"
        )

        got = await _read(client, name)
        assert got["vcs-repo-url"] == "https://gitlab.com/myorg/safe", (
            "the refusal did not roll back"
        )

    async def test_an_in_scope_url_only_patch_still_works(self, app, client):
        """The refusal must not block a legitimate move within the allowlist."""
        set_auth(app, admin_user())
        cid = await _connection(client, ["myorg/*"])
        name = f"m-{uuid.uuid4().hex[:8]}"
        assert (await _create(client, name, cid, "https://gitlab.com/myorg/safe")).status_code in (
            200,
            201,
        )

        r = await _patch(client, name, {"vcs-repo-url": "https://gitlab.com/myorg/other"})
        assert r.status_code == 200, r.text

    async def test_a_url_only_patch_on_a_module_with_no_connection_is_unaffected(self, app, client):
        set_auth(app, admin_user())
        name = f"m-{uuid.uuid4().hex[:8]}"
        assert (await _create(client, name)).status_code in (200, 201)
        r = await _patch(client, name, {"vcs-repo-url": "https://gitlab.com/anyone/anything"})
        assert r.status_code == 200, r.text
