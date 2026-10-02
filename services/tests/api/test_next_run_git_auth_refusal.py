"""runs/next refuses a GitLab VCS-connection git credential, visibly.

A GitLab VCS connection stores a Personal or Group Access Token an operator
pasted in, and nothing produces a narrower copy of one — so a `git_http_auth`
variable sourced from it hands the token to a runner Job whole, with every
permission and every project it covers, and the connection is chosen in a
variable *value* rather than by an admin. It is therefore gated on
`api.config.vcs.gitlab.allow_token_delivery_to_runners`, off by default.

What this file pins is the half the service tier cannot see: with the switch
off the run is **errored with the reason**, not served a payload missing the
credential. A silently absent credential would leave `init` to fail somewhere
that names neither the credential nor the cause — exactly what the Vault
resolver above it already refuses to do — and the operator who set the switch
would get no signal at all.

Driven through the real `next_run` handler with the real git-auth resolver;
only the claim, the workspace/connection loads and the transition are mocked.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers import runs as runs_router
from terrapod.config import settings
from terrapod.db.models import VCSConnection
from terrapod.services.variable_service import ResolvedVariable

TOKEN = "glpat-NEVER-IN-A-MESSAGE"
KEY = "gitlab.example.com/platform"


def _gitlab_cred(ref: str) -> ResolvedVariable:
    return ResolvedVariable(
        key=KEY,
        value=json.dumps(
            {"source": "vcs_connection", "vcs_connection_id": ref, "rewrite": "to_https"}
        ),
        category="git_http_auth",
        structured=False,
        sensitive=True,
    )


class _Claim:
    def __init__(self, resp, transition):
        self.resp = resp
        self.transition = transition

    @property
    def errored_with(self) -> str | None:
        """The message the run was errored with, or None if it was not."""
        for call in self.transition.mock_calls:
            if len(call.args) >= 3 and call.args[2] == "errored":
                return call.kwargs.get("error_message", "")
        return None


async def _claim(resolved, *, provider="gitlab") -> _Claim:
    lid = uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.source = "tfe-api"
    ws = MagicMock()
    ws.var_files = []
    ws.working_directory = ""
    ws.engine = "terraform"
    ws.name = "smoke"
    ws.pulumi_bind_plan = False
    # `server_url` must be real and must match KEY's host: a minted credential is
    # only installed for its own connection's host, and a MagicMock's attribute is a
    # Mock, which resolves to no host at all and is refused.
    conn = MagicMock(provider=provider, token=TOKEN, server_url="https://gitlab.example.com")

    async def _get(model, _id):
        # One `db.get` serves two lookups: the workspace, and the VCS connection
        # the git-auth resolver dereferences. Dispatch on the model so the real
        # resolver runs against a real-shaped connection.
        return conn if model is VCSConnection else ws

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.add_all = MagicMock()
    transition = AsyncMock()
    with (
        patch.object(
            runs_router.agent_pool_service,
            "get_listener",
            AsyncMock(return_value={"pool_id": str(uuid.uuid4()), "name": "l"}),
        ),
        patch.object(
            runs_router.run_service, "claim_next_run", AsyncMock(return_value=(run, "plan"))
        ),
        patch.object(runs_router.run_service, "transition_run", transition),
        patch(
            "terrapod.services.variable_service.resolve_variables",
            AsyncMock(return_value=resolved),
        ),
        patch("terrapod.config.load_runner_config", return_value=MagicMock(hooks_enabled=False)),
        patch.object(
            runs_router, "_run_json", return_value={"data": {"id": "run-x", "attributes": {}}}
        ),
    ):
        resp = await runs_router.next_run(
            listener_id=f"listener-{lid}", identity=MagicMock(listener_id=lid), db=db
        )
    return _Claim(resp, transition)


class TestTheSwitchIsOff:
    """The shipped default."""

    async def test_the_run_is_errored_with_the_reason_not_served_without_it(self):
        c = await _claim([_gitlab_cred(f"vcs-{uuid.uuid4()}")])
        msg = c.errored_with
        assert msg is not None, "the run was served a payload with the credential missing"
        assert KEY in msg
        assert "api.config.vcs.gitlab.allow_token_delivery_to_runners" in msg
        assert "static" in msg

    async def test_the_listener_gets_no_run_rather_than_a_500(self):
        """A 500 leaves the run claimed and the listener retrying a wedged run."""
        c = await _claim([_gitlab_cred(f"vcs-{uuid.uuid4()}")])
        assert c.resp.status_code == 204

    async def test_the_token_is_in_neither_the_payload_nor_the_error(self):
        c = await _claim([_gitlab_cred(f"vcs-{uuid.uuid4()}")])
        assert TOKEN not in (c.errored_with or "")
        assert TOKEN not in (c.resp.body or b"").decode()


class TestTheSwitchIsOn:
    async def test_the_credential_is_delivered_and_the_run_is_not_errored(self):
        with patch.object(settings.vcs.gitlab, "allow_token_delivery_to_runners", True):
            c = await _claim([_gitlab_cred(f"vcs-{uuid.uuid4()}")])
        assert c.errored_with is None
        assert c.resp.status_code == 200
        delivered = json.loads(c.resp.body)["data"]["attributes"]["git-auth"]
        assert json.loads(delivered[0]["value"])["token"] == TOKEN


pytestmark = pytest.mark.asyncio
