"""`pulumi_config` end to end against real Postgres (#1565).

The mocked tiers prove the shapes. Two things here can only be proved against a
real engine, and both are the kind that pass a mock and fail in production:

- **the engine-mismatch query.** `_resolve_inert_var_ws` is one statement with a
  join, a coalesce-and-lower over the engine column and an OR of per-category
  clauses, resolved once per request so the workspace list keeps its O(page)
  fast path (#1056). An `AsyncMock` returns whatever it was told to; only a real
  database says whether the SQL selects the right rows — and gets the empty and
  all-clean cases right rather than lighting up every workspace.
- **that the category survives a round trip.** It is validated, stored and read
  back through the same column every other category uses, so a missing entry in
  `VALID_CATEGORIES` or a length constraint would show up here.
"""

import pytest

from terrapod.config import settings
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration


#: Workspaces are created on the NATIVE surface, because only it can express an
#: engine — the TFE route pins Terraform by design, since a CLI client there
#: could not see a workspace belonging to another engine even if it could make
#: one (#1535). The list and read below are the TFE surface, which serves both.
#: Both create and read on the native surface, which is the one that serves
#: every engine.
WORKSPACES = "/api/terrapod/v1/workspaces"


@pytest.fixture(autouse=True)
def _pulumi_enabled():
    """The engine gate filters `known_engines()`, so creating a Pulumi
    workspace is refused outright while Pulumi is off — which is the gate doing
    its job, and means these tests have to turn it on."""
    before = settings.engines.pulumi.enabled
    settings.engines.pulumi.enabled = True
    yield
    settings.engines.pulumi.enabled = before


async def _workspace(client, name: str, engine: str = "terraform") -> str:
    body = {
        "data": {
            "type": "workspaces",
            "attributes": {"name": name, "engine": engine},
        }
    }
    resp = await client.post(WORKSPACES, json=body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _var(client, ws_id: str, key: str, category: str, **extra) -> dict:
    attrs = {"key": key, "value": "v", "category": category}
    attrs.update(extra)
    resp = await client.post(
        f"/api/v2/workspaces/{ws_id}/vars",
        json={"data": {"type": "vars", "attributes": attrs}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def _conditions(client, ws_id: str) -> list[str]:
    """Read on the NATIVE surface, which serves every engine.

    The TFE read route is pinned to Terraform by `_engine_filter`, so a Pulumi
    workspace 404s there rather than rendering — deliberately, since that route
    is the CLI compatibility contract and a client on it could not act on a
    workspace belonging to another engine.
    """
    resp = await client.get(f"/api/terrapod/v1/workspaces/{ws_id}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return [c["code"] for c in resp.json()["data"]["attributes"]["health-conditions"]]


class TestTheCategoryRoundTrips:
    async def test_it_is_stored_and_read_back(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, "pc-roundtrip::dev", engine="pulumi")
        created = await _var(client, ws, "aws:region", "pulumi_config")
        assert created["attributes"]["category"] == "pulumi_config"
        # Verbatim, including the namespace: nothing splits or prefixes it.
        assert created["attributes"]["key"] == "aws:region"

        resp = await client.get(f"/api/v2/workspaces/{ws}/vars", headers=AUTH)
        assert resp.status_code == 200
        keys = {v["attributes"]["key"]: v["attributes"] for v in resp.json()["data"]}
        assert keys["aws:region"]["category"] == "pulumi_config"

    async def test_it_is_accepted_on_a_terraform_workspace(self, app, client):
        set_auth(app, admin_user())
        """#1407 §6: engine mismatch is permissive. Variables are data, and
        which of them apply is decided at run time by the engine that runs."""
        ws = await _workspace(client, "pc-permissive", engine="terraform")
        created = await _var(client, ws, "region", "pulumi_config")
        assert created["attributes"]["category"] == "pulumi_config"


class TestTheMismatchIsReportedFromRealRows:
    async def test_a_variable_the_engine_never_reads_reports_false(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, "pc-inert", engine="terraform")
        created = await _var(client, ws, "region", "pulumi_config")
        assert created["attributes"]["applies-to-engine"] is False

    async def test_one_the_engine_does_read_reports_true(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, "pc-live::dev", engine="pulumi")
        created = await _var(client, ws, "region", "pulumi_config")
        assert created["attributes"]["applies-to-engine"] is True

    async def test_the_rule_is_symmetric(self, app, client):
        set_auth(app, admin_user())
        """A workspace that changed engine keeps its old variables, and they are
        just as inert in that direction."""
        ws = await _workspace(client, "pc-symmetric::dev", engine="pulumi")
        created = await _var(client, ws, "cidr", "terraform")
        assert created["attributes"]["applies-to-engine"] is False

    async def test_an_engine_neutral_category_always_applies(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, "pc-neutral::dev", engine="pulumi")
        created = await _var(client, ws, "AWS_REGION", "env")
        assert created["attributes"]["applies-to-engine"] is True


class TestTheHealthConditionComesFromTheQuery:
    async def test_a_workspace_holding_an_inert_variable_raises_it(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, "pc-cond-on", engine="terraform")
        assert "variables_not_consumed" not in await _conditions(client, ws)
        await _var(client, ws, "region", "pulumi_config")
        assert "variables_not_consumed" in await _conditions(client, ws)

    async def test_a_workspace_whose_variables_all_apply_does_not(self, app, client):
        set_auth(app, admin_user())
        """The case a too-broad OR would break: every workspace lighting up is
        as useless as none of them doing so."""
        ws = await _workspace(client, "pc-cond-off::dev", engine="pulumi")
        await _var(client, ws, "region", "pulumi_config")
        await _var(client, ws, "AWS_REGION", "env")
        assert "variables_not_consumed" not in await _conditions(client, ws)

    async def test_a_workspace_with_no_variables_does_not(self, app, client):
        set_auth(app, admin_user())
        ws = await _workspace(client, "pc-cond-none::dev", engine="pulumi")
        assert "variables_not_consumed" not in await _conditions(client, ws)

    async def test_the_list_endpoint_flags_only_the_workspace_that_holds_one(self, app, client):
        set_auth(app, admin_user())
        """The query is resolved once for the whole page and then read per row,
        so the failure to watch for is one workspace's variable flagging its
        neighbours."""
        dirty = await _workspace(client, "pc-list-dirty", engine="terraform")
        clean = await _workspace(client, "pc-list-clean", engine="terraform")
        await _var(client, dirty, "region", "pulumi_config")
        await _var(client, clean, "cidr", "terraform")

        resp = await client.get(f"{WORKSPACES}?page[size]=100", headers=AUTH)
        assert resp.status_code == 200, resp.text
        by_id = {w["id"]: w["attributes"]["health-conditions"] for w in resp.json()["data"]}
        assert "variables_not_consumed" in [c["code"] for c in by_id[dirty]]
        assert "variables_not_consumed" not in [c["code"] for c in by_id[clean]]
