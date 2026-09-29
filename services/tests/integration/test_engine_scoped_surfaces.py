"""One workspace, two answers, decided by the door (#1572).

The TFE surface serves `terraform` / `tofu` / `tfci` and must never hand them a
row from another engine — `_engine_filter`'s own docstring calls that "a silent
wrong answer, not an error". Terrapod's own surface has the opposite job: it
serves every engine, because that is what the UI and the provider talk to.

Before this, the rule held in `tfe_v2.py` and in none of the other eight router
mounts on that surface, so one workspace answered both ways at once:

    GET /api/tfe/v2/workspaces/{id}        404   (filtered)
    GET /api/tfe/v2/workspaces/{id}/runs   200   (not filtered)

A client told the workspace does not exist, then handed its runs.

Integration rather than mocked, deliberately: the property is "a real row with
`engine='pulumi'` is invisible through one prefix and visible through another",
and a mocked session has no rows and no prefixes. The source-introspection guard
in `tests/api/test_v2_engine_filter.py` is the other half — it catches a query
that loses its filter; this catches the behaviour being wrong anyway.
"""

import pytest

from terrapod.config import settings
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WORKSPACES = "/api/terrapod/v1/workspaces"

#: Every prefix serving the TFE compatibility surface. Both, because the alias
#: is what every client in the field already holds — a fix on the canonical one
#: alone would leave the leak exactly where the traffic is.
TFE_PREFIXES = ("/api/tfe/v2", "/api/v2")

#: And Terrapod's own, which must keep answering for every engine.
NATIVE_PREFIXES = ("/api/v1", "/api/terrapod/v1")


@pytest.fixture(autouse=True)
def _pulumi_enabled():
    before = settings.engines.pulumi.enabled
    settings.engines.pulumi.enabled = True
    yield
    settings.engines.pulumi.enabled = before


async def _workspace(client, name: str, engine: str) -> str:
    resp = await client.post(
        WORKSPACES,
        json={"data": {"type": "workspaces", "attributes": {"name": name, "engine": engine}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


class TestAPulumiWorkspaceIsInvisibleOnTheCompatibilitySurface:
    @pytest.mark.parametrize("prefix", TFE_PREFIXES)
    @pytest.mark.parametrize("sub", ["", "/runs", "/configuration-versions", "/vars"])
    async def test_every_sub_resource_agrees_with_the_workspace(self, app, client, prefix, sub):
        """The failure this replaces was not "one route was wrong" — it was that
        the routes disagreed. So the assertion is on all of them together."""
        set_auth(app, admin_user())
        ws = await _workspace(client, "scoped-pulumi::dev", "pulumi")

        resp = await client.get(f"{prefix}/workspaces/{ws}{sub}", headers=AUTH)
        assert resp.status_code == 404, (
            f"{prefix}/workspaces/{{id}}{sub} served a Pulumi workspace to the "
            f"TFE surface ({resp.status_code}) — a `terraform` client cannot "
            f"parse it, and gets no error saying so"
        )


class TestTheSameWorkspaceIsVisibleOnTerrapodsOwnSurface:
    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    async def test_the_workspace_itself(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _workspace(client, "scoped-native::dev", "pulumi")
        resp = await client.get(f"{prefix}/workspaces/{ws}", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["engine"] == "pulumi"

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    @pytest.mark.parametrize("sub", ["/runs", "/configuration-versions", "/vars"])
    async def test_and_its_sub_resources(self, app, client, prefix, sub):
        """The reason the native mounts exist. Without them the scoping above
        would simply take the Pulumi UI away."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"scoped-sub{sub.replace('/', '-')}::dev", "pulumi")
        resp = await client.get(f"{prefix}/workspaces/{ws}{sub}", headers=AUTH)
        assert resp.status_code == 200, f"{prefix}{sub}: {resp.text}"


class TestTerraformIsUnaffectedEverywhere:
    """The whole constraint on this work: multi-engine costs Terraform nothing."""

    @pytest.mark.parametrize("prefix", TFE_PREFIXES + NATIVE_PREFIXES)
    @pytest.mark.parametrize("sub", ["", "/runs", "/configuration-versions", "/vars"])
    async def test_a_terraform_workspace_answers_on_every_prefix(self, app, client, prefix, sub):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"scoped-tf{sub.replace('/', '-') or '-self'}", "terraform")
        resp = await client.get(f"{prefix}/workspaces/{ws}{sub}", headers=AUTH)
        assert resp.status_code == 200, f"{prefix}{sub}: {resp.text}"
