"""One category, two wire names, against real Postgres (#1898).

The mocked tiers prove the shapes. Three things here can only be proved against
a real engine, and each is the kind that passes a mock and fails in production:

- **the alias folds before the row is looked up.** `terraform` and
  `pulumi_config` are both accepted on input and both mean `native`. If the fold
  happened after the lookup, a second write under another name would try to
  INSERT a second `native:region` — which the widened unique constraint forbids,
  so the operator would be told their own variable already exists. Only a real
  constraint can fail that way.
- **the column really holds the canonical name.** A mock stores whatever it is
  handed; a real column, with real validation and a real length limit, is what
  says the stored value is `native`.
- **each surface serves the name its clients hold**, read back from a row that
  was actually written rather than from a serializer called directly.

A Pulumi workspace appears here because it is the case that motivated the
collapse: its stack config and a Terraform workspace's input variables are the
same role, so they are the same category, and the workspace's engine decides the
delivery rather than the category doing it.
"""

import pytest

from terrapod.config import settings
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration


#: Workspaces are created on the NATIVE surface, because only it can express an
#: engine — the TFE route pins Terraform by design, since a CLI client there
#: could not see a workspace belonging to another engine even if it could make
#: one (#1535).
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
    resp = await client.post(
        WORKSPACES,
        json={"data": {"type": "workspaces", "attributes": {"name": name, "engine": engine}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _var(
    client, ws_id: str, key: str, category: str, value: str = "v", prefix: str = "/api/v2"
):
    """Write a variable. `prefix` matters for a PULUMI workspace (#1572).

    The TFE-compatible surface serves Terraform only, so a Pulumi workspace is
    not reachable through it at all — its variables are written on Terrapod's own
    surface. Defaults to `/api/v2` because most of this file is about the two
    names one category wears, which is a Terraform-workspace question.
    """
    return await client.post(
        f"{prefix}/workspaces/{ws_id}/vars",
        json={
            "data": {
                "type": "vars",
                "attributes": {"key": key, "value": value, "category": category},
            }
        },
        headers=AUTH,
    )


async def _vars(client, ws_id: str, prefix: str = "/api/v2") -> dict:
    resp = await client.get(f"{prefix}/workspaces/{ws_id}/vars", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return {v["attributes"]["key"]: v["attributes"] for v in resp.json()["data"]}


class TestEveryAcceptedNameLandsOnOneRow:
    @pytest.mark.parametrize("written_as", ["native", "terraform", "pulumi_config"])
    async def test_each_name_is_accepted(self, app, client, written_as) -> None:
        """All three are accepted for ever. `terraform` because `tfci` and
        `go-tfe` send it; `pulumi_config` because it existed for about two hours
        (#1565) and refusing it would break a `terraform_variable` resource
        written in that window for no gain."""
        set_auth(app, admin_user())
        ws = await _workspace(client, f"nc-accepts-{written_as.replace('_', '-')}")
        resp = await _var(client, ws, "region", written_as)
        assert resp.status_code == 201, resp.text

    @pytest.mark.parametrize(
        "first,second", [("terraform", "pulumi_config"), ("native", "terraform")]
    )
    async def test_two_names_for_it_collide(self, app, client, first, second) -> None:
        """POST is create, so a second one under another of the category's names
        is a duplicate — and answering 409 is the proof the fold happens at the
        write boundary rather than after the row is found.

        Folding late would make the second call an INSERT of a row the first
        call's row does not appear to be, and the widened `(owner, key,
        category)` constraint would be the only thing left to catch it. It
        would catch it — as a database error on a path that thinks it is
        creating something new, which is a worse answer arrived at by accident.
        """
        set_auth(app, admin_user())
        ws = await _workspace(client, f"nc-collide-{first}-{second}".replace("_", "-"))

        assert (await _var(client, ws, "region", first, "eu-west-1")).status_code == 201
        dup = await _var(client, ws, "region", second, "us-east-1")
        assert dup.status_code == 409, dup.text

    async def test_and_the_row_is_left_exactly_as_it_was(self, app, client) -> None:
        """The refusal must not be a partial write: one row, still holding the
        value the first call set."""
        set_auth(app, admin_user())
        ws = await _workspace(client, "nc-one-row")

        assert (await _var(client, ws, "region", "terraform", "eu-west-1")).status_code == 201
        assert (await _var(client, ws, "region", "pulumi_config", "us-east-1")).status_code == 409

        rows = await _vars(client, ws)
        assert list(rows) == ["region"]
        assert rows["region"]["value"] == "eu-west-1"


class TestEachSurfaceServesTheNameItsClientsHold:
    async def test_the_compatibility_surface_says_terraform(self, app, client) -> None:
        set_auth(app, admin_user())
        ws = await _workspace(client, "nc-tfe-name")
        assert (await _var(client, ws, "region", "native")).status_code == 201
        assert (await _vars(client, ws, "/api/v2"))["region"]["category"] == "terraform"

    async def test_the_native_surface_says_native(self, app, client) -> None:
        set_auth(app, admin_user())
        ws = await _workspace(client, "nc-native-name")
        assert (await _var(client, ws, "region", "terraform")).status_code == 201
        assert (await _vars(client, ws, "/api/v1"))["region"]["category"] == "native"

    async def test_the_key_is_stored_verbatim(self, app, client) -> None:
        """Pulumi's namespaced form has to survive the round trip: the CLI
        namespaces an unqualified key to the project itself, and `aws:region`
        means a provider setting. Nothing here splits or prefixes it."""
        set_auth(app, admin_user())
        ws = await _workspace(client, "nc-verbatim::dev", engine="pulumi")
        assert (await _var(client, ws, "aws:region", "native", prefix="/api/v1")).status_code == 201
        assert "aws:region" in await _vars(client, ws, "/api/v1")


class TestTheCategoryIsTheSameOnEveryEngine:
    """The collapse's whole claim: a Pulumi workspace's stack config and a
    Terraform workspace's input variables are one role, so they are one
    category, and nothing about the write depends on the engine."""

    @pytest.mark.parametrize("engine", ["terraform", "pulumi"])
    async def test_the_same_write_works_on_either(self, app, client, engine) -> None:
        set_auth(app, admin_user())
        # Pulumi names a workspace `project::stack`; Terraform does not.
        name = "nc-engine-pulumi::dev" if engine == "pulumi" else "nc-engine-terraform"
        ws = await _workspace(client, name, engine=engine)
        # Native surface for both: it is the one that serves every engine, which
        # is the point of the test.
        assert (await _var(client, ws, "region", "native", prefix="/api/v1")).status_code == 201
        assert (await _vars(client, ws, "/api/v1"))["region"]["category"] == "native"

    async def test_env_is_untouched_by_the_collapse(self, app, client) -> None:
        """`env` is a different role — it reaches the process environment
        whatever engine runs — and must keep its own name on both surfaces."""
        set_auth(app, admin_user())
        ws = await _workspace(client, "nc-env::dev", engine="pulumi")
        assert (await _var(client, ws, "AWS_REGION", "env", prefix="/api/v1")).status_code == 201
        # Only the native surface — a Pulumi workspace is not on the other one
        # (#1572), so reading it there would be asserting the leak.
        assert (await _vars(client, ws, "/api/v1"))["AWS_REGION"]["category"] == "env"
