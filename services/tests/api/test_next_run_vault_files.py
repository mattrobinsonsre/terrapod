"""runs/next with Vault file delivery (#1619).

Drives the real `next_run` handler with its collaborators mocked at the service
boundary: the resolver, the substitution and the payload are all real. The
properties pinned here:

- a file-mode variable's delivered value is the file's path, for env and
  terraform categories alike, and its content appears **only** in `vault-files`;
- a listener that predates `vault-files` therefore cannot put the content into
  env or tfvars — the API has no fallback that would let it;
- a clash, an oversized file or hcl errors the run; an unavailable Vault puts it
  back in the queue; neither returns a 500 or leaves the run claimed;
- nothing the API logs carries the content.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.api.routers import runs as runs_router
from terrapod.config import VaultConfig, settings
from terrapod.services.variable_service import ResolvedVariable
from terrapod.services.vault_client import VaultError, VaultUnavailable

SECRET = "S3CR3T-next-run-file-content"


def _ref(**kw) -> str:
    base = {"source": "vault", "mount": "kvv2", "path": "apps/gcp", "field": "sa"}
    base.update(kw)
    return json.dumps(base)


def _rv(key, value, *, category="env", value_source="vault", hcl=False):
    return ResolvedVariable(
        key=key,
        value=value,
        category=category,
        hcl=hcl,
        sensitive=True,
        value_source=value_source,
    )


@pytest.fixture(autouse=True)
def _vault_on():
    prior = settings.vault
    settings.vault = VaultConfig(
        enabled=True, instances=[{"name": "default", "address": "https://v"}]
    )
    yield
    settings.vault = prior


class _Claim:
    def __init__(self, resp, transition, run, db, read, logs):
        self.resp = resp
        self.transition = transition
        self.run = run
        self.db = db
        self.read = read
        self.logs = logs

    @property
    def attrs(self) -> dict:
        assert self.resp.status_code == 200, self.resp.body
        return json.loads(self.resp.body)["data"]["attributes"]


async def _claim(resolved, *, read=None) -> _Claim:
    lid = uuid.uuid4()
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.source = "tfe-api"
    ws = MagicMock()
    ws.var_files = []
    ws.working_directory = ""
    db = AsyncMock()
    db.get = AsyncMock(return_value=ws)
    transition = AsyncMock()
    read = read or AsyncMock(return_value={"sa": SECRET, "token": "TOKEN-V"})
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
        patch("terrapod.services.git_auth_service.resolve_git_auth", AsyncMock(return_value=[])),
        patch("terrapod.config.load_runner_config", return_value=MagicMock(hooks_enabled=False)),
        patch.object(
            runs_router, "_run_json", return_value={"data": {"id": "run-x", "attributes": {}}}
        ),
        patch("terrapod.services.vault_source_service.read_secret_data", read),
        patch.object(runs_router, "logger") as api_log,
        patch("terrapod.services.vault_source_service.logger") as vss_log,
    ):
        resp = await runs_router.next_run(
            listener_id=f"listener-{lid}", identity=MagicMock(listener_id=lid), db=db
        )
    return _Claim(
        resp, transition, run, db, read, str(api_log.mock_calls) + str(vss_log.mock_calls)
    )


def _without_vault_files(attrs: dict) -> str:
    return json.dumps({k: v for k, v in attrs.items() if k != "vault-files"})


class TestDelivery:
    async def test_file_variables_carry_their_path_and_the_content_rides_vault_files(self):
        c = await _claim(
            [
                _rv("GOOGLE_APPLICATION_CREDENTIALS", _ref(file={"name": "gcp/adc.json"})),
                _rv(
                    "sa_file", _ref(file={"name": "~/.config/gcloud/sa.json"}), category="terraform"
                ),
                _rv("PLAIN", "literal", value_source="static"),
            ]
        )
        attrs = c.attrs
        env = {v["key"]: v["value"] for v in attrs["env-vars"]}
        assert env == {
            "GOOGLE_APPLICATION_CREDENTIALS": "/var/run/terrapod/files/gcp/adc.json",
            "PLAIN": "literal",
        }
        assert attrs["terraform-vars"] == [
            {"key": "sa_file", "value": "/home/runner/.config/gcloud/sa.json", "hcl": False}
        ]
        assert attrs["vault-files"] == [
            {"key": "GOOGLE_APPLICATION_CREDENTIALS", "name": "gcp/adc.json", "value": SECRET},
            {"key": "sa_file", "name": "~/.config/gcloud/sa.json", "value": SECRET},
        ]
        # Both reference the same secret: one read, one credential.
        assert c.read.await_count == 1
        c.transition.assert_not_awaited()

    async def test_the_content_appears_nowhere_but_vault_files(self):
        c = await _claim(
            [
                _rv("ENV_FILE", _ref(file={})),
                _rv("tf_file", _ref(file={"name": "tf.json"}), category="terraform"),
            ]
        )
        assert SECRET in json.dumps(c.attrs["vault-files"])
        assert SECRET not in _without_vault_files(c.attrs)
        assert SECRET not in c.logs

    async def test_an_older_listener_that_ignores_vault_files_gets_only_paths(self):
        """Fail-safe under version skew: an older listener builds its Secret from
        env-vars and terraform-vars alone. Reproduce exactly what it reads."""
        c = await _claim(
            [
                _rv("ENV_FILE", _ref(file={})),
                _rv("tf_file", _ref(file={}), category="terraform"),
            ]
        )
        attrs = c.attrs
        old_env = [{"key": v["key"], "value": v["value"]} for v in attrs.get("env-vars", [])]
        old_tf = [
            {"key": v["key"], "value": v["value"], "hcl": bool(v.get("hcl"))}
            for v in attrs.get("terraform-vars", [])
        ]
        delivered = json.dumps({"env": old_env, "tfvars": old_tf})
        assert SECRET not in delivered
        assert {v["value"] for v in old_env + old_tf} == {
            "/var/run/terrapod/files/ENV_FILE",
            "/var/run/terrapod/files/tf_file",
        }

    async def test_an_ordinary_vault_variable_is_unchanged(self):
        c = await _claim([_rv("TOKEN", _ref(field="token"))])
        assert c.attrs["env-vars"] == [{"key": "TOKEN", "value": "TOKEN-V"}]
        assert c.attrs["vault-files"] == []

    async def test_a_workspace_with_no_vault_variables_has_empty_vault_files(self):
        c = await _claim([_rv("PLAIN", "literal", value_source="static")])
        assert c.attrs["vault-files"] == []
        c.read.assert_not_awaited()


class TestTheRunFailsOrWaits:
    async def _errored_with(self, c) -> str:
        assert c.resp.status_code == 204
        c.transition.assert_awaited_once()
        assert c.transition.await_args.args[1:] == (c.run, "errored")
        return c.transition.await_args.kwargs["error_message"]

    async def test_two_variables_at_one_path_error_the_run_without_reading_vault(self):
        c = await _claim(
            [
                _rv("A_SET_VAR", _ref(file={"name": "creds.json"})),
                _rv("B_WS_VAR", _ref(file={"name": "creds.json"}, path="apps/other")),
            ]
        )
        msg = await self._errored_with(c)
        assert msg == (
            "variables 'A_SET_VAR' and 'B_WS_VAR' both deliver a Vault file to "
            "/var/run/terrapod/files/creds.json"
        )
        c.read.assert_not_awaited()

    async def test_a_file_under_another_files_path_errors_the_run(self):
        c = await _claim([_rv("A", _ref(file={"name": "a"})), _rv("B", _ref(file={"name": "a/b"}))])
        msg = await self._errored_with(c)
        assert "needs as a directory for /var/run/terrapod/files/a/b" in msg

    async def test_an_oversized_file_errors_the_run(self):
        big = AsyncMock(return_value={"sa": "Z" * (256 * 1024 + 1)})
        c = await _claim([_rv("F", _ref(file={}))], read=big)
        msg = await self._errored_with(c)
        assert msg == (
            "variable 'F': the Vault value is 262145 bytes, over the 256 KiB limit for a file"
        )

    async def test_hcl_on_a_file_variable_errors_the_run(self):
        c = await _claim([_rv("f", _ref(file={}), category="terraform", hcl=True)])
        msg = await self._errored_with(c)
        assert "hcl enabled" in msg

    async def test_a_4xx_in_file_mode_errors_the_run(self):
        denied = AsyncMock(side_effect=VaultError("Vault denied 'kvv2/apps/gcp'"))
        c = await _claim([_rv("F", _ref(file={}))], read=denied)
        msg = await self._errored_with(c)
        assert msg == "variable 'F': Vault denied 'kvv2/apps/gcp'"

    async def test_an_unavailable_vault_in_file_mode_requeues_the_run(self):
        down = AsyncMock(side_effect=VaultUnavailable("HTTP 503"))
        c = await _claim([_rv("F", _ref(file={}))], read=down)
        assert c.resp.status_code == 204
        c.transition.assert_awaited_once()
        assert c.transition.await_args.args[1:] == (c.run, "queued")

    async def test_no_failure_path_logs_the_content(self):
        big = AsyncMock(return_value={"sa": SECRET * 20000})
        c = await _claim([_rv("F", _ref(file={}))], read=big)
        assert c.resp.status_code == 204
        assert SECRET not in c.logs
        assert SECRET not in c.transition.await_args.kwargs["error_message"]
