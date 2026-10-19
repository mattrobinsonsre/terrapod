"""A workspace may not name an execution environment this deployment cannot run.

#2154 D7, driven through the real write routes against a real database, because
the behaviour is a **relationship between two states** and a mocked session would
let the fixture decide the answer:

* the available set is **known** and the line is absent -> 422
* the available set is **not known** -> accepted

Those have opposite consequences and the second is the one an obvious fixture
never reaches, because an obvious fixture has a list. Refusing there would make
an ordinary workspace edit depend on registry reachability, which is an outage
mode invented by a validation check rather than by anything the operator did.

Nothing here reaches a registry: the availability read is the seam, patched per
test to assert the decision rather than the plumbing. The plumbing has its own
tests, including ones that drive a real socket.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import ansible_ee_service as ee
from tests.integration.conftest import admin_user, set_auth

pytestmark = pytest.mark.integration

WS = "/api/v2/organizations/default/workspaces"
CT = {"Content-Type": "application/vnd.api+json"}


def _available(lines, environments=None):
    return patch.object(
        ee, "availability", AsyncMock(return_value=ee.Availability(lines, environments or {}))
    )


def _name() -> str:
    return f"ee-{uuid.uuid4().hex[:8]}"


async def _create(client, **attrs):
    return await client.post(
        WS,
        json={"data": {"type": "workspaces", "attributes": {"name": _name(), **attrs}}},
        headers=CT,
    )


class TestNamingALine:
    async def test_an_unpublished_line_is_refused_naming_what_is_available(self, app, client):
        set_auth(app, admin_user())
        with _available(["2.21", "2.12"]):
            r = await _create(client, **{"ansible-version": "2.99"})
        assert r.status_code == 422, r.text
        assert "2.99" in r.text and "2.21" in r.text

    async def test_a_published_line_is_accepted(self, app, client):
        """A guard that refuses legitimate work gets switched off, so this carries
        as much weight as the refusal."""
        set_auth(app, admin_user())
        with _available(["2.21", "2.12"]):
            r = await _create(client, **{"ansible-version": "2.12"})
        assert r.status_code in (200, 201), r.text
        assert r.json()["data"]["attributes"]["ansible-version"] == "2.12"

    async def test_an_unknown_set_ACCEPTS_the_same_line_it_would_refuse(self, app, client):
        """The contrast that matters, and the reason this file is a pair rather
        than a single case: identical input, opposite outcome, decided only by
        whether Terrapod could read the set."""
        set_auth(app, admin_user())
        with _available(None):
            r = await _create(client, **{"ansible-version": "2.99"})
        assert r.status_code in (200, 201), r.text

    async def test_naming_no_line_never_consults_the_registry(self, app, client):
        """The common case by a wide margin. Create resolves an absent key to the
        platform's line, so consulting the set here would make every ordinary
        workspace create wait on a listing to confirm a line Terrapod chose."""
        set_auth(app, admin_user())
        with patch.object(ee, "availability", AsyncMock()) as avail:
            r = await _create(client)
        assert r.status_code in (200, 201), r.text
        avail.assert_not_awaited()


class TestNamingAnOperatorEnvironment:
    async def test_an_unconfigured_label_is_always_refused(self, app, client, monkeypatch):
        """Local configuration, so there is no "cannot tell" -- and the set being
        unreadable must not turn an unconfigured label into an accepted one."""
        set_auth(app, admin_user())
        monkeypatch.setattr(settings.ansible, "execution_environments", {})
        with _available(None):
            r = await _create(client, **{"ansible-execution-environment": "nope"})
        assert r.status_code == 422, r.text
        assert "nope" in r.text

    async def test_a_configured_label_is_accepted_and_stored_verbatim(
        self, app, client, monkeypatch
    ):
        set_auth(app, admin_user())
        monkeypatch.setattr(
            settings.ansible, "execution_environments", {"rhel8-hardened": "r/ee:1"}
        )
        with _available(["2.21"]):
            r = await _create(client, **{"ansible-execution-environment": "rhel8-hardened"})
        assert r.status_code in (200, 201), r.text
        attrs = r.json()["data"]["attributes"]
        # Byte for byte: a provider writes the response back into state, and a
        # normalised value fails the apply with "Provider produced inconsistent
        # result after apply".
        assert attrs["ansible-execution-environment"] == "rhel8-hardened"

    async def test_a_label_makes_an_unpublished_line_beside_it_irrelevant(
        self, app, client, monkeypatch
    ):
        """The label overrides the line, so the line selects nothing and must not
        be refused. Getting this wrong would refuse a valid workspace for a value
        it does not use."""
        set_auth(app, admin_user())
        monkeypatch.setattr(settings.ansible, "execution_environments", {"h": "r/ee:1"})
        with _available(["2.21"]):
            r = await _create(
                client,
                **{"ansible-version": "2.99", "ansible-execution-environment": "h"},
            )
        assert r.status_code in (200, 201), r.text


class TestEditingAnExistingWorkspace:
    async def test_a_patch_of_something_else_does_not_re_validate_the_stored_line(
        self, app, client
    ):
        """#316's trap. A workspace whose line stopped being published must stay
        editable: re-validating a stored value would trap the row, leaving it
        changeable only by the route that refuses it."""
        set_auth(app, admin_user())
        with _available(["2.21"]):
            created = await _create(client, **{"ansible-version": "2.21"})
        assert created.status_code in (200, 201), created.text
        ws_id = created.json()["data"]["id"]

        # The line is gone from the set now, and the PATCH says nothing about it.
        with _available(["2.12"]):
            r = await client.patch(
                f"/api/v2/workspaces/{ws_id}",
                json={"data": {"type": "workspaces", "attributes": {"auto-apply": True}}},
                headers=CT,
            )
        assert r.status_code == 200, r.text

    async def test_a_patch_naming_an_unpublished_line_is_refused(self, app, client):
        set_auth(app, admin_user())
        with _available(["2.21"]):
            created = await _create(client)
        ws_id = created.json()["data"]["id"]
        with _available(["2.21"]):
            r = await client.patch(
                f"/api/v2/workspaces/{ws_id}",
                json={"data": {"type": "workspaces", "attributes": {"ansible-version": "2.99"}}},
                headers=CT,
            )
        assert r.status_code == 422, r.text
