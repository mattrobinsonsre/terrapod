"""`stack export` serves a secrets-provider URL a client can reach (#1580).

`test_pulumi_state_service.py` pins the normalisation itself. This pins that
the endpoint **reaches** it, which is the half that silently rots: a helper
with its own passing tests, wired to nothing, looks exactly like a fix.

The endpoint matters specifically because `export` is how the CLI reads state
before an update, so it is the path a stale URL actually breaks.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap
from terrapod.db.session import get_db

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/pulumi/api"
MOD = "terrapod.api.routers.pulumi_service"

CANONICAL = "https://terrapod.example.com/api/v1/pulumi"
#: What an agent run wrote before #1576: the runner's in-cluster API address.
STALE = "http://terrapod-api:8000/api/terrapod/v1/pulumi"


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="someone@example.test",
        display_name="Someone",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _ws() -> MagicMock:
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.name = "proj::dev"
    ws.labels = {}
    ws.engine = "pulumi"
    ws.updated_at = None
    return ws


def _deployment(url: str) -> dict:
    return {
        "resources": [{"urn": "urn:pulumi:dev::proj::x::r"}],
        "secrets_providers": {
            "type": "service",
            "state": {"url": url, "owner": "default", "project": "proj", "stack": "dev"},
        },
    }


def _runner() -> AuthenticatedUser:
    """A runner token — an agent run's own CLI, which reaches the API directly."""
    return AuthenticatedUser(
        email="runner",
        display_name="Runner Job",
        roles=["everyone"],
        provider_name="runner_token",
        auth_method="runner_token",
        run_id=str(uuid.uuid4()),
    )


async def _export(
    stored: dict | None, *, external_url: str | None, runner: bool = False, prefix: str = BASE
) -> dict:
    """One real request through the app, with storage and config controlled."""
    from terrapod.api.app import create_application
    from terrapod.api.routers.pulumi_service import pulumi_user

    ws = _ws()

    async def _find(db, stack_id):  # noqa: ANN001
        return ws

    async def _caps(db, user, w, **_):  # noqa: ANN001
        return frozenset({cap.WORKSPACE_READ, cap.STATE_READ})

    app = create_application()
    app.dependency_overrides[pulumi_user] = (lambda: _runner()) if runner else (lambda: _user())
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    with (
        patch(f"{MOD}._find_stack", _find),
        patch(f"{MOD}.resolve_workspace_capabilities_for", _caps),
        # A runner resolves through its own allowance, not role RBAC (#1880), so
        # the role-based patch above never applies to it. This file is about the
        # URL in the block; what a runner may do is pinned in
        # `test_pulumi_runner_authz.py`.
        patch(f"{MOD}._runner_caps_on", _caps),
        patch(f"{MOD}._read_deployment", AsyncMock(return_value=stored)),
        patch(f"{MOD}.settings") as cfg,
    ):
        cfg.external_url = external_url
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            resp = await c.get(f"{prefix}/stacks/default/proj/dev/export")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestExportServesAReachableServiceUrl:
    async def test_a_stale_in_cluster_url_is_not_what_the_client_receives(self):
        """The bug: the CLI takes this URL literally, looks up a credential for
        it, and cannot be helped by logging in — the address is cluster-only."""
        body = await _export(_deployment(STALE), external_url="https://terrapod.example.com")
        assert body["deployment"]["secrets_providers"]["state"]["url"] == CANONICAL

    async def test_the_state_itself_is_served_unchanged(self):
        body = await _export(_deployment(STALE), external_url="https://terrapod.example.com")
        assert body["deployment"]["resources"] == [{"urn": "urn:pulumi:dev::proj::x::r"}]
        state = body["deployment"]["secrets_providers"]["state"]
        assert (state["owner"], state["project"], state["stack"]) == ("default", "proj", "dev")

    async def test_without_an_external_url_the_stored_block_is_served_as_is(self):
        """No declared address is no opinion — see `with_canonical_service_url`."""
        body = await _export(_deployment(STALE), external_url=None)
        assert body["deployment"]["secrets_providers"]["state"]["url"] == STALE

    async def test_a_stack_with_no_state_still_answers_null(self):
        """The CLI's snapshot integrity check rejects a synthetic empty
        deployment, so this contract must survive the normalisation."""
        body = await _export(None, external_url="https://terrapod.example.com")
        assert body["deployment"] is None


ALIAS = "/api/terrapod/v1/pulumi/api"


class TestARunnerReadsABlockNamingItsOwnBackend:
    """Pulumi's secrets manager looks up a credential for the URL **in the stored
    state**, so a caller that is handed somebody else's address cannot open the
    stack at all. Normalising every reader to `external_url` is what broke the
    second agent run on every stack the first one had written (#1887):

        could not find access token for https://terrapod.local/api/v1/pulumi,
        have you logged in?

    A runner reaches the API directly, with no BFF in between, so its own request
    names the backend it has configured.
    """

    async def test_it_gets_its_own_host_not_the_declared_one(self):
        body = await _export(
            _deployment(STALE), external_url="https://terrapod.example.com", runner=True
        )
        assert body["deployment"]["secrets_providers"]["state"]["url"] == "http://t/api/v1/pulumi"

    async def test_it_gets_the_prefix_it_actually_called(self):
        """The runner uses the deprecated alias deliberately — only that one is
        served on both sides of the N-2 skew guarantee — so the block has to name
        the alias too, not the canonical prefix."""
        body = await _export(
            _deployment(STALE),
            external_url="https://terrapod.example.com",
            runner=True,
            prefix=ALIAS,
        )
        assert (
            body["deployment"]["secrets_providers"]["state"]["url"]
            == "http://t/api/terrapod/v1/pulumi"
        )

    async def test_a_person_still_gets_the_declared_address(self):
        """Their request has come through the BFF, so its host is an internal one
        and useless to them. This is the half #1580 got right."""
        body = await _export(_deployment(STALE), external_url="https://terrapod.example.com")
        assert body["deployment"]["secrets_providers"]["state"]["url"] == CANONICAL

    async def test_the_state_itself_is_untouched_for_a_runner(self):
        body = await _export(
            _deployment(STALE), external_url="https://terrapod.example.com", runner=True
        )
        assert body["deployment"]["resources"] == [{"urn": "urn:pulumi:dev::proj::x::r"}]
