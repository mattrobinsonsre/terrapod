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


async def _runnable(client, name: str, engine: str) -> str:
    """A workspace with an uploaded configuration version, so a run can exist.

    Created on the NATIVE surface throughout — the compatibility one refuses a
    Pulumi workspace, which is the property under test.
    """
    ws = await _workspace(client, name, engine)
    resp = await client.post(
        f"/api/v1/workspaces/{ws}/configuration-versions",
        json={"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    upload = await client.put(
        resp.json()["data"]["attributes"]["upload-url"],
        content=b"placeholder-tarball-for-tests",
        headers={"Content-Type": "application/x-tar"},
    )
    assert upload.status_code in (200, 204), upload.text
    return ws


async def _run_on(client, ws: str) -> str:
    resp = await client.post(
        "/api/v1/runs",
        json={
            "data": {
                "type": "runs",
                "attributes": {"plan-only": True},
                "relationships": {"workspace": {"data": {"type": "workspaces", "id": ws}}},
            }
        },
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


class TestARunIsScopedLikeItsWorkspace:
    """#1904. The workspace fix did not cover runs: the guard sees
    `select(Model)`, and a run is reached by primary key through the service
    layer, so `GET /api/tfe/v2/runs/{id}` went on answering 200 for a Pulumi run
    whose own workspace answered 404 on the same surface.

    The check lives at `_require_run_ws_capability`, which every run handler
    already calls — one place rather than twenty-six, and a handler that skipped
    it would have a far louder problem than an engine leak.
    """

    @pytest.mark.parametrize("prefix", TFE_PREFIXES)
    async def test_a_pulumi_run_is_not_served_to_the_compatibility_surface(
        self, app, client, prefix
    ):
        set_auth(app, admin_user())
        ws = await _runnable(client, f"scoped-run{len(prefix)}::dev", "pulumi")
        run = await _run_on(client, ws)
        resp = await client.get(f"{prefix}/runs/{run}", headers=AUTH)
        assert resp.status_code == 404, (
            f"{prefix}/runs/{{id}} served a Pulumi run ({resp.status_code}) — a "
            f"`terraform` client will try to parse it and gets no error saying so"
        )

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    async def test_but_terrapods_own_surface_serves_it(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _runnable(client, f"scoped-nrun{len(prefix)}::dev", "pulumi")
        run = await _run_on(client, ws)
        resp = await client.get(f"{prefix}/runs/{run}", headers=AUTH)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("prefix", TFE_PREFIXES + NATIVE_PREFIXES)
    async def test_a_terraform_run_is_served_everywhere(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _runnable(client, f"scoped-tfrun{len(prefix)}", "terraform")
        run = await _run_on(client, ws)
        assert (await client.get(f"{prefix}/runs/{run}", headers=AUTH)).status_code == 200

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    @pytest.mark.parametrize("action", ["apply", "cancel"])
    async def test_a_mutating_route_still_works_on_the_native_surface(
        self, app, client, prefix, action
    ):
        """A read is not enough to prove the check reads the surface.

        The check is keyed on the request's path, so a handler that takes a
        `Request` and forgets to hand it over falls into the "no request" case —
        and the only safe reading of that is "apply the check", which 404s a
        Pulumi run on the surface that exists to serve it. `confirm_run` did
        precisely this: it grew the parameter and never passed it, which no read
        test could see because reads were already threaded.

        Asserted as a bound rather than an exact status, because what these
        answer depends on the run's state — the point is only that the engine
        check let them through to find out. 5xx is excluded because `request` is
        now a required keyword there, so the other shape of the same mistake —
        omitting it entirely — surfaces as a TypeError rather than a 404.
        """
        set_auth(app, admin_user())
        ws = await _runnable(client, f"scoped-mut{action}{len(prefix)}::dev", "pulumi")
        run = await _run_on(client, ws)
        resp = await client.post(f"{prefix}/runs/{run}/actions/{action}", headers=AUTH)
        assert resp.status_code != 404 and resp.status_code < 500, (
            f"{prefix}/runs/{{id}}/actions/{action} answered {resp.status_code} "
            f"for a Pulumi run on Terrapod's own surface — the handler is not "
            f"telling the engine check which surface asked: {resp.text}"
        )
