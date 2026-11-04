"""Integration tests: a variable set is visible by workspace reach (#2379).

Virgin ground before this: there was no RBAC-negative test for variable sets
anywhere, because there was no RBAC on them to test. All three read routes
authenticated and then authorized nothing, so any principal who could log in
could enumerate every set in the deployment -- and through
`/relationships/vars`, every variable KEY and `sensitive` flag in each. The
values were masked; the inventory was not, and for a set carrying a credential
the inventory is most of the disclosure.

Driven here against real role rows, real labels and real assignments in
Postgres, because the thing under test is the resolution. The router helper's
own branches (the see-all fast path, the global short-circuit, the memoisation)
are driven with mocks in `tests/api/test_variables.py::TestVarsetVisibility`.

Two fixture hazards this file is built around, both of which would make a
negative case pass for the wrong reason:

* the built-in `everyone` role grants on `access=everyone`, so an unreadable
  workspace must not carry that label;
* a workspace's OWNER holds full capabilities on it, so an unreadable workspace
  must be created by a different principal than the one doing the reading.
"""

import pytest

from tests.integration.conftest import (
    AUTH,
    admin_user,
    assign_role,
    insert_role,
    regular_user,
    set_auth,
    user_with_roles,
)

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
VS_ENDPOINT = "/api/v2/organizations/default/varsets"


async def _workspace(client, name: str, labels: dict | None = None) -> str:
    attrs: dict = {"name": name}
    if labels is not None:
        attrs["labels"] = labels
    resp = await client.post(
        WS_ENDPOINT, json={"data": {"type": "workspaces", "attributes": attrs}}, headers=AUTH
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _varset(client, name: str, **attrs) -> str:
    resp = await client.post(
        VS_ENDPOINT,
        json={"data": {"type": "varsets", "attributes": {"name": name, **attrs}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _assign(client, vs_id: str, ws_id: str) -> None:
    resp = await client.post(
        f"/api/v2/varsets/{vs_id}/relationships/workspaces",
        json={"data": [{"id": ws_id, "type": "workspaces"}]},
        headers=AUTH,
    )
    assert resp.status_code == 204, resp.text


async def _add_var(client, vs_id: str, key: str, value: str, sensitive: bool = False) -> None:
    resp = await client.post(
        f"/api/v2/varsets/{vs_id}/relationships/vars",
        json={
            "data": {
                "type": "vars",
                "attributes": {"key": key, "value": value, "sensitive": sensitive},
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text


async def _names(client) -> list[str]:
    resp = await client.get(VS_ENDPOINT, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return [d["attributes"]["name"] for d in resp.json()["data"]]


async def _reader(engine, client, *, email: str, role: str, labels: dict):
    """A principal whose only reach is a label-scoped read role."""
    await insert_role(engine, role, workspace_permission="read", allow_labels=labels)
    await assign_role(engine, "local", email, role)
    return user_with_roles(email, [role, "everyone"])


class TestTheListIsFilteredByReach:
    async def test_a_set_reaching_only_an_unreadable_workspace_is_withheld(
        self, app, client, _create_tables
    ):
        """The disclosure this exists to close. Note the admin creates the
        workspace, so the reader is not its owner, and it carries no
        `access=everyone` label -- either would hand the reader capabilities and
        make this pass for a reason that has nothing to do with the filter."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-1@test.com"))
        hidden_ws = await _workspace(client, "vsvis-hidden-1", labels={"tier": "secret"})
        vs = await _varset(client, "vsvis-hidden-set-1")
        await _assign(client, vs, hidden_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-1@test.com",
            role="vsvis-role-1",
            labels={"tier": "open"},
        )
        set_auth(app, reader)
        assert "vsvis-hidden-set-1" not in await _names(client)

    async def test_a_set_reaching_a_readable_workspace_is_listed(self, app, client, _create_tables):
        """The other half of the pair. Either test alone passes whichever way the
        filter is written -- the property is a relationship between two states,
        so both have to be instantiated."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-2@test.com"))
        open_ws = await _workspace(client, "vsvis-open-2", labels={"tier": "open2"})
        vs = await _varset(client, "vsvis-open-set-2")
        await _assign(client, vs, open_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-2@test.com",
            role="vsvis-role-2",
            labels={"tier": "open2"},
        )
        set_auth(app, reader)
        assert "vsvis-open-set-2" in await _names(client)

    async def test_one_readable_workspace_out_of_two_is_enough(self, app, client, _create_tables):
        """Reach is ANY, not ALL: a set spanning the estate must not be hidden
        from someone who can read one of the workspaces it feeds."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-3@test.com"))
        open_ws = await _workspace(client, "vsvis-open-3", labels={"tier": "open3"})
        hidden_ws = await _workspace(client, "vsvis-hidden-3", labels={"tier": "secret3"})
        vs = await _varset(client, "vsvis-spanning-set-3")
        await _assign(client, vs, open_ws)
        await _assign(client, vs, hidden_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-3@test.com",
            role="vsvis-role-3",
            labels={"tier": "open3"},
        )
        set_auth(app, reader)
        assert "vsvis-spanning-set-3" in await _names(client)

    async def test_an_admin_sees_a_set_reaching_nothing_it_can_read_by_label(
        self, app, client, _create_tables
    ):
        """The see-all path, end to end: an unassigned set reaches no workspace at
        all, so under the reach rule alone it would be invisible to everybody and
        the admin page would be empty."""
        set_auth(app, admin_user("vsvis-admin-4@test.com"))
        await _varset(client, "vsvis-unassigned-set-4")
        assert "vsvis-unassigned-set-4" in await _names(client)

    async def test_an_unassigned_set_is_invisible_to_a_regular_user(
        self, app, client, _create_tables
    ):
        """And the converse, which is the honest consequence of the rule: a set
        reaching nothing is visible to nobody but a see-all principal. Recorded
        as a test so the behaviour is a decision rather than a surprise."""
        set_auth(app, admin_user("vsvis-admin-5@test.com"))
        await _varset(client, "vsvis-unassigned-set-5")
        set_auth(app, regular_user("vsvis-nobody-5@test.com"))
        assert "vsvis-unassigned-set-5" not in await _names(client)

    async def test_a_global_set_is_visible_to_a_regular_user(self, app, client, _create_tables):
        """A global set reaches the whole estate and names no particular
        workspace, so it is visible without a reach query."""
        set_auth(app, admin_user("vsvis-admin-6@test.com"))
        await _varset(client, "vsvis-global-set-6", **{"global": True})
        set_auth(app, regular_user("vsvis-nobody-6@test.com"))
        assert "vsvis-global-set-6" in await _names(client)

    async def test_a_rule_matched_set_follows_the_matcher(self, app, client, _create_tables):
        """Reach covers an assignment RULE, not only an explicit assignment --
        and it is computed by the same function the injector uses, so this filter
        cannot disagree with the matcher about who receives a credential."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-7@test.com"))
        await _workspace(client, "vsvis-ruled-7", labels={"tier": "ruled7"})
        await _varset(
            client,
            "vsvis-ruled-set-7",
            **{"assignment-rule": {"labels": {"tier": "ruled7"}}},
        )

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-7@test.com",
            role="vsvis-role-7",
            labels={"tier": "ruled7"},
        )
        set_auth(app, reader)
        assert "vsvis-ruled-set-7" in await _names(client)

    async def test_the_count_is_the_visible_count(self, app, client, _create_tables):
        """The filter runs before `paginate`, so `total-count` is what the caller
        can see. Reporting the row count would disclose how many sets exist,
        which is most of what the filter exists to withhold."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-8@test.com"))
        hidden_ws = await _workspace(client, "vsvis-hidden-8", labels={"tier": "secret8"})
        for i in range(3):
            vs = await _varset(client, f"vsvis-hidden-set-8-{i}")
            await _assign(client, vs, hidden_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-8@test.com",
            role="vsvis-role-8",
            labels={"tier": "open8"},
        )
        set_auth(app, reader)
        resp = await client.get(VS_ENDPOINT, headers=AUTH)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        visible = [d["attributes"]["name"] for d in body["data"]]
        assert not [n for n in visible if n.startswith("vsvis-hidden-set-8")]
        assert body["meta"]["pagination"]["total-count"] == len(visible)


class TestTheByIdRoutesAreGatedToo:
    async def test_an_invisible_set_is_404_by_id(self, app, client, _create_tables):
        """404, not 403: a distinguishable 403 would still answer "does this id
        exist" for a principal with no reach, which a list filter cannot close."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-9@test.com"))
        hidden_ws = await _workspace(client, "vsvis-hidden-9", labels={"tier": "secret9"})
        vs = await _varset(client, "vsvis-hidden-set-9")
        await _assign(client, vs, hidden_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-9@test.com",
            role="vsvis-role-9",
            labels={"tier": "open9"},
        )
        set_auth(app, reader)
        resp = await client.get(f"/api/v2/varsets/{vs}", headers=AUTH)
        assert resp.status_code == 404, resp.text

    async def test_a_visible_set_is_still_readable_by_id(self, app, client, _create_tables):
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-10@test.com"))
        open_ws = await _workspace(client, "vsvis-open-10", labels={"tier": "open10"})
        vs = await _varset(client, "vsvis-open-set-10")
        await _assign(client, vs, open_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-10@test.com",
            role="vsvis-role-10",
            labels={"tier": "open10"},
        )
        set_auth(app, reader)
        resp = await client.get(f"/api/v2/varsets/{vs}", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["name"] == "vsvis-open-set-10"

    async def test_the_variable_keys_of_an_invisible_set_are_404(self, app, client, _create_tables):
        """The route that disclosed most, and the reason the by-id gate is not
        merely tidiness: it lists every key and `sensitive` flag in the set."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-11@test.com"))
        hidden_ws = await _workspace(client, "vsvis-hidden-11", labels={"tier": "secret11"})
        vs = await _varset(client, "vsvis-hidden-set-11")
        await _add_var(client, vs, "prod_db_password", "s3cr3t", sensitive=True)
        await _assign(client, vs, hidden_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-11@test.com",
            role="vsvis-role-11",
            labels={"tier": "open11"},
        )
        set_auth(app, reader)
        resp = await client.get(f"/api/v2/varsets/{vs}/relationships/vars", headers=AUTH)
        assert resp.status_code == 404, resp.text
        assert "prod_db_password" not in resp.text

    async def test_the_variable_keys_of_a_visible_set_are_served(self, app, client, _create_tables):
        """The guard must not break the route it protects."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-12@test.com"))
        open_ws = await _workspace(client, "vsvis-open-12", labels={"tier": "open12"})
        vs = await _varset(client, "vsvis-open-set-12")
        await _add_var(client, vs, "region", "eu-west-1")
        await _assign(client, vs, open_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-12@test.com",
            role="vsvis-role-12",
            labels={"tier": "open12"},
        )
        set_auth(app, reader)
        resp = await client.get(f"/api/v2/varsets/{vs}/relationships/vars", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert [d["attributes"]["key"] for d in resp.json()["data"]] == ["region"]

    async def test_the_blast_radius_view_stays_admin_only(self, app, client, _create_tables):
        """`/relationships/workspaces` lists EVERY workspace a set reaches, so
        extending the new rule to it would turn it into a leak: visible because
        you can read one of them, then naming the ones you cannot. It keeps
        `require_admin`, and that is a decision rather than an omission."""
        engine = _create_tables
        set_auth(app, admin_user("vsvis-admin-13@test.com"))
        open_ws = await _workspace(client, "vsvis-open-13", labels={"tier": "open13"})
        vs = await _varset(client, "vsvis-open-set-13")
        await _assign(client, vs, open_ws)

        reader = await _reader(
            engine,
            client,
            email="vsvis-reader-13@test.com",
            role="vsvis-role-13",
            labels={"tier": "open13"},
        )
        set_auth(app, reader)
        resp = await client.get(
            f"/api/terrapod/v1/varsets/{vs}/relationships/workspaces", headers=AUTH
        )
        assert resp.status_code == 403, resp.text
