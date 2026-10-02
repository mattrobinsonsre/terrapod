"""A deployment may hold more than one GitLab connection, and still only one per
GitHub App installation.

`uq_vcs_connections_install` was a blanket `UNIQUE (provider,
github_installation_id)` from the initial schema. `github_installation_id` is
`NOT NULL DEFAULT 0` and means nothing on a GitLab row, so every GitLab connection
carried `0` and the second one collided with the first — reported as a bare `409
Resource already exists or violates a constraint`, naming neither the column nor
the reason.

Integration tier, not services-api: the rule is a Postgres index, and a mocked
session cannot tell a partial unique index from a blanket one. These tests fail
against the old constraint and pass against the new.
"""

import uuid

from tests.integration.conftest import admin_user, set_auth

CONNS = "/api/terrapod/v1/vcs-connections"
CT = {"Content-Type": "application/vnd.api+json"}


def _gitlab(name: str, allowed: list[str] | None = None) -> dict:
    attrs: dict = {"name": name, "provider": "gitlab", "token": f"glpat-{name}"}
    if allowed is not None:
        attrs["allowed-repositories"] = allowed
    return {"data": {"type": "vcs-connections", "attributes": attrs}}


def _github(name: str, installation_id: int) -> dict:
    return {
        "data": {
            "type": "vcs-connections",
            "attributes": {
                "name": name,
                "provider": "github",
                "github-app-id": 4242,
                "github-installation-id": installation_id,
                "private-key": "-----BEGIN RSA PRIVATE KEY-----\nnot-a-key\n-----END RSA PRIVATE KEY-----",
            },
        }
    }


class TestMoreThanOneGitLabConnection:
    async def test_two_gitlab_connections_can_coexist(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]

        first = await client.post(CONNS, json=_gitlab(f"gl-a-{tag}"), headers=CT)
        assert first.status_code in (200, 201), first.text

        second = await client.post(CONNS, json=_gitlab(f"gl-b-{tag}"), headers=CT)
        assert second.status_code in (200, 201), (
            "a second GitLab connection was refused — the blanket unique "
            f"constraint is back, and the operator sees only: {second.text}"
        )
        assert first.json()["data"]["id"] != second.json()["data"]["id"]

    async def test_and_a_third_with_an_allowlist_too(self, app, client):
        """The shape the end-to-end allowlist test needs: one open, one restricted.

        It is a contrast, so it cannot be written with a single connection — which
        is why this defect went eighteen months without anyone meeting it.
        """
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]

        openc = await client.post(CONNS, json=_gitlab(f"gl-open-{tag}", []), headers=CT)
        assert openc.status_code in (200, 201), openc.text
        shut = await client.post(
            CONNS, json=_gitlab(f"gl-shut-{tag}", ["example/infra-*"]), headers=CT
        )
        assert shut.status_code in (200, 201), shut.text

        assert openc.json()["data"]["attributes"]["allowed-repositories"] == []
        assert shut.json()["data"]["attributes"]["allowed-repositories"] == ["example/infra-*"]


class TestOneConnectionPerGitHubInstallation:
    """The half the constraint was actually for, which must not have been lost.

    Without this, scoping the index to GitHub would look like a pass while having
    removed the rule entirely.
    """

    async def test_the_same_installation_twice_is_refused(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        inst = 900_000 + (uuid.uuid4().int % 90_000)

        first = await client.post(CONNS, json=_github(f"gh-a-{tag}", inst), headers=CT)
        assert first.status_code in (200, 201), first.text

        again = await client.post(CONNS, json=_github(f"gh-b-{tag}", inst), headers=CT)
        assert again.status_code == 422, (
            "a second connection for the same GitHub App installation was "
            f"accepted; two credentials now cover the same repositories: {again.text}"
        )

    async def test_a_different_installation_is_fine(self, app, client):
        set_auth(app, admin_user())
        tag = uuid.uuid4().hex[:8]
        base = 800_000 + (uuid.uuid4().int % 90_000)

        a = await client.post(CONNS, json=_github(f"gh-c-{tag}", base), headers=CT)
        assert a.status_code in (200, 201), a.text
        b = await client.post(CONNS, json=_github(f"gh-d-{tag}", base + 1), headers=CT)
        assert b.status_code in (200, 201), b.text
