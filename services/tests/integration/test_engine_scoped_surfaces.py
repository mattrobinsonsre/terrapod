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


# ── The routes that had no native door at all (#1911) ─────────────────────────
#
# Scoping the compatibility surface is only half the job. Locking a workspace and
# reading its state history are not Terraform concepts — every engine Terrapod
# runs has state, and a lock protecting it — but those routes existed *only* on
# the TFE surface. So the scoping above did not hide a Pulumi workspace's state
# tab from a `terraform` client; it took the tab away from the operator. The UI's
# State tab errored and its padlock 404d, and the capability was there the whole
# time with no door.
#
# `dual_router` is that door. These tests assert both halves at once, because
# only asserting the new one would pass just as well if the mount had *moved*
# instead of being added — which is the change that breaks every CLI in the field.


async def _pulumi_state_version(client, ws: str) -> str:
    """Upload a minimal `pulumi stack export` and return its state-version id.

    The native manual-upload route, because the TFE create/upload pair is the
    `go-tfe` protocol and refuses another engine's state by design.
    """
    resp = await client.post(
        f"/api/v1/workspaces/{ws}/state-versions/actions/upload",
        content=b'{"version": 3, "deployment": {"manifest": {"time": "2026-01-01T00:00:00Z"}}}',
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]["id"]


class TestLockingAPulumiWorkspace:
    """The padlock. `workspace.locked` is the manual state lock, and a Pulumi
    stack's state needs protecting from a concurrent operator exactly as a
    Terraform workspace's does."""

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    async def test_lock_then_unlock_on_the_native_surface(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"lock-native-{prefix.count('/')}::dev", "pulumi")

        locked = await client.post(f"{prefix}/workspaces/{ws}/actions/lock", json={}, headers=AUTH)
        assert locked.status_code == 200, locked.text
        assert locked.json()["data"]["attributes"]["locked"] is True

        unlocked = await client.post(
            f"{prefix}/workspaces/{ws}/actions/unlock", json={}, headers=AUTH
        )
        assert unlocked.status_code == 200, unlocked.text
        assert unlocked.json()["data"]["attributes"]["locked"] is False

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    async def test_force_unlock_too(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"forceunlock-{prefix.count('/')}::dev", "pulumi")
        await client.post(f"{prefix}/workspaces/{ws}/actions/lock", json={}, headers=AUTH)
        resp = await client.post(
            f"{prefix}/workspaces/{ws}/actions/force-unlock", json={}, headers=AUTH
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["locked"] is False

    @pytest.mark.parametrize("prefix", TFE_PREFIXES)
    @pytest.mark.parametrize("action", ["lock", "unlock", "force-unlock"])
    async def test_but_the_compatibility_surface_still_refuses(self, app, client, prefix, action):
        """Mounted on both, so the risk is that the TFE mount quietly started
        serving Pulumi too. It must not."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"lock-tfe-{action}-{prefix.count('/')}::dev", "pulumi")
        resp = await client.post(
            f"{prefix}/workspaces/{ws}/actions/{action}", json={}, headers=AUTH
        )
        assert resp.status_code == 404, f"{prefix} {action}: {resp.status_code} {resp.text}"

    @pytest.mark.parametrize("prefix", TFE_PREFIXES + NATIVE_PREFIXES)
    async def test_and_terraform_locks_on_every_prefix(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"lock-tf-{prefix.count('/')}-{len(prefix)}", "terraform")
        resp = await client.post(f"{prefix}/workspaces/{ws}/actions/lock", json={}, headers=AUTH)
        assert resp.status_code == 200, f"{prefix}: {resp.text}"


class TestReadingAPulumiWorkspacesStateHistory:
    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    async def test_the_list_and_the_current_version(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"sv-native-{prefix.count('/')}::dev", "pulumi")
        sv = await _pulumi_state_version(client, ws)

        listing = await client.get(f"{prefix}/workspaces/{ws}/state-versions", headers=AUTH)
        assert listing.status_code == 200, listing.text
        assert [row["id"] for row in listing.json()["data"]] == [sv]

        current = await client.get(f"{prefix}/workspaces/{ws}/current-state-version", headers=AUTH)
        assert current.status_code == 200, current.text
        assert current.json()["data"]["id"] == sv

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    async def test_the_version_itself_and_its_bytes(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"svshow-{prefix.count('/')}::dev", "pulumi")
        sv = await _pulumi_state_version(client, ws)

        shown = await client.get(f"{prefix}/state-versions/{sv}", headers=AUTH)
        assert shown.status_code == 200, shown.text
        assert shown.json()["data"]["id"] == sv

        blob = await client.get(f"{prefix}/state-versions/{sv}/download", headers=AUTH)
        assert blob.status_code == 200, blob.text
        assert b"manifest" in blob.content

    @pytest.mark.parametrize("prefix", TFE_PREFIXES)
    @pytest.mark.parametrize("sub", ["/state-versions", "/current-state-version"])
    async def test_the_compatibility_surface_sees_none_of_it(self, app, client, prefix, sub):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"svtfe{sub.replace('/', '-')}{len(prefix)}::dev", "pulumi")
        await _pulumi_state_version(client, ws)
        resp = await client.get(f"{prefix}/workspaces/{ws}{sub}", headers=AUTH)
        assert resp.status_code == 404, f"{prefix}{sub}: {resp.status_code}"

    @pytest.mark.parametrize("prefix", TFE_PREFIXES)
    @pytest.mark.parametrize("sub", ["", "/download"])
    async def test_nor_by_the_state_versions_own_id(self, app, client, prefix, sub):
        """The leak the workspace filter never covered.

        `select(StateVersion)` names a model the engine guard did not watch, and
        the owning workspace was then loaded by primary key *from the row that
        query had already returned* — which reads as derived-and-therefore-safe.
        So `/workspaces/{id}` 404d a Pulumi workspace while `/state-versions/{sv}`
        handed a `terraform` client its state.
        """
        set_auth(app, admin_user())
        ws = await _workspace(client, f"svid{sub.strip('/') or 'show'}{len(prefix)}::dev", "pulumi")
        sv = await _pulumi_state_version(client, ws)
        resp = await client.get(f"{prefix}/state-versions/{sv}{sub}", headers=AUTH)
        assert resp.status_code == 404, (
            f"{prefix}/state-versions/{{id}}{sub} served a Pulumi workspace's state "
            f"to the TFE surface ({resp.status_code})"
        )


class TestLookingAWorkspaceUpByName:
    """The CLI deep-link resolver (`/app/{org}/{name}`) reached for the TFE route,
    so it resolved a Terraform workspace and fell back to the list for a Pulumi one.

    The server was never the gap: `GET /api/v1/workspaces/{ref}` already takes an
    id **or** a name for every enabled engine, because a Pulumi workspace is
    addressed by its `project::stack` name everywhere a person meets it. Nothing
    pinned that, though — it was a documented behaviour with no test, which is how
    a survey of the routes concluded it did not exist and nearly added a second
    path for the same lookup. So this pins it.
    """

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    @pytest.mark.parametrize("engine", ["terraform", "pulumi"])
    async def test_the_native_route_finds_every_engine(self, app, client, prefix, engine):
        set_auth(app, admin_user())
        # A Pulumi workspace is named `project::stack`, so the name carries a
        # `::` — which this route has to survive in a path segment. Asserting it
        # with the real shape rather than a tidy one is the point.
        name = f"byname-{engine}-{len(prefix)}" + ("::dev" if engine == "pulumi" else "")
        ws = await _workspace(client, name, engine)
        resp = await client.get(f"{prefix}/workspaces/{name}", headers=AUTH)
        assert resp.status_code == 200, f"{prefix} {engine}: {resp.text}"
        assert resp.json()["data"]["id"] == ws
        assert resp.json()["data"]["attributes"]["engine"] == engine

    @pytest.mark.parametrize("prefix", TFE_PREFIXES)
    async def test_the_tfe_route_still_finds_only_terraform(self, app, client, prefix):
        set_auth(app, admin_user())
        tf = f"bynametfe-tf-{len(prefix)}"
        pu = f"bynametfe-pu-{len(prefix)}::dev"
        await _workspace(client, tf, "terraform")
        await _workspace(client, pu, "pulumi")

        found = await client.get(f"{prefix}/organizations/default/workspaces/{tf}", headers=AUTH)
        assert found.status_code == 200, found.text
        missing = await client.get(f"{prefix}/organizations/default/workspaces/{pu}", headers=AUTH)
        assert missing.status_code == 404, missing.text

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    async def test_an_unknown_name_is_a_404_not_an_empty_list(self, app, client, prefix):
        set_auth(app, admin_user())
        resp = await client.get(f"{prefix}/workspaces/nothing-by-this-name", headers=AUTH)
        assert resp.status_code == 404, resp.text


class TestPulumiBindPlanStaysOffTheTerraformPinnedWire:
    """The one Pulumi concept that reached the compatibility surface.

    It could only ever be `false` there, which is why it went unnoticed — but
    "harmless" is a property of today's value, not of the attribute. Gated on the
    door, not on `ws.engine`, so the native representation is unchanged for every
    engine and a consumer already reading the key keeps getting it.
    """

    @pytest.mark.parametrize("prefix", TFE_PREFIXES)
    async def test_absent_on_the_compatibility_surface(self, app, client, prefix):
        set_auth(app, admin_user())
        ws = await _workspace(client, f"bindplan-tfe-{len(prefix)}", "terraform")
        resp = await client.get(f"{prefix}/workspaces/{ws}", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert "pulumi-bind-plan" not in resp.json()["data"]["attributes"]

    @pytest.mark.parametrize("prefix", NATIVE_PREFIXES)
    @pytest.mark.parametrize("engine", ["terraform", "pulumi"])
    async def test_but_present_natively_for_every_engine(self, app, client, prefix, engine):
        set_auth(app, admin_user())
        name = f"bindplan-{engine}-{len(prefix)}" + ("::dev" if engine == "pulumi" else "")
        ws = await _workspace(client, name, engine)
        resp = await client.get(f"{prefix}/workspaces/{ws}", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert "pulumi-bind-plan" in resp.json()["data"]["attributes"]
