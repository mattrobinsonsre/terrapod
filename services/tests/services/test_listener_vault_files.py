"""The listener's side of Vault file delivery (#1619).

It receives each file's content in `vault-files`, writes it as a key of the
per-run vars Secret, and hands the Job builder names and Secret keys only. It
re-validates every name, refuses a Secret key an env variable already uses, and
never logs a value.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import terrapod.runner.listener as listener_module
from tests.services.test_listener import _make_listener

RunnerListener = listener_module.RunnerListener


@pytest.fixture(autouse=True)
def fresh_shutdown_event():
    """The module-level shutdown event, rebound to the test's loop (as in
    test_listener.py, whose fixture this mirrors)."""
    import asyncio

    new_event = asyncio.Event()
    old_event = listener_module._shutdown
    listener_module._shutdown = new_event
    yield new_event
    listener_module._shutdown = old_event


SECRET = "S3CR3T-listener-file-content-must-not-leak"


def _files(*names):
    return [{"key": f"V{i}", "name": n, "value": f"{SECRET}-{i}"} for i, n in enumerate(names)]


class TestPlanVaultFiles:
    def test_mounts_carry_names_only_and_values_are_keyed_for_the_secret(self):
        mounts, values = RunnerListener._plan_vault_files(
            _files("gcp/adc.json", "~/.aws/credentials"), env_vars=[]
        )
        assert mounts == [
            {"name": "gcp/adc.json", "secret_key": "vault-file-0"},
            {"name": "~/.aws/credentials", "secret_key": "vault-file-1"},
        ]
        assert values == {"vault-file-0": f"{SECRET}-0", "vault-file-1": f"{SECRET}-1"}
        assert SECRET not in json.dumps(mounts)

    def test_nothing_to_do_without_files(self):
        assert RunnerListener._plan_vault_files([], env_vars=[]) == ([], {})
        assert RunnerListener._plan_vault_files(None, env_vars=[]) == ([], {})

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("../x", "has a '.' or '..' path segment"),
            ("/etc/passwd", "absolute paths are not allowed"),
            ("~/.ssh/id_rsa", "which the runner manages itself"),
            ("", "must not be empty"),
        ],
    )
    def test_an_invalid_name_is_refused_again_here(self, name, reason):
        with pytest.raises(ValueError) as e:
            RunnerListener._plan_vault_files(
                [{"key": "F", "name": name, "value": SECRET}], env_vars=[]
            )
        assert str(e.value).startswith(f"variable 'F': file name {name!r} is invalid: ")
        assert reason in str(e.value)
        assert SECRET not in str(e.value)

    def test_a_collision_is_refused_again_here(self):
        with pytest.raises(ValueError, match="both deliver an OpenBao/Vault file"):
            RunnerListener._plan_vault_files(_files("a", "a"), env_vars=[])

    def test_an_env_variable_using_the_derived_key_is_refused(self):
        with pytest.raises(ValueError) as e:
            RunnerListener._plan_vault_files(
                _files("a.json"), env_vars=[{"key": "vault-file-0", "value": "x"}]
            )
        assert str(e.value) == (
            "env variable 'vault-file-0' clashes with the Secret key Terrapod uses for the "
            "OpenBao/Vault file of variable 'V0'; rename the env variable"
        )


class TestVarsSecretCarriesTheFiles:
    async def test_each_file_is_a_secret_key_beside_env_and_tfvars(self, fresh_shutdown_event):
        listener = _make_listener(fresh_shutdown_event)
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-plan-vars",
                "r1",
                terraform_vars=[{"key": "f", "value": "/var/run/terrapod/files/f", "hcl": False}],
                env_vars=[{"key": "GOOGLE_APPLICATION_CREDENTIALS", "value": "/var/run/x"}],
                job_name="tprun-r1-plan",
                job_uid="uid-1",
                vault_file_values={"vault-file-0": SECRET},
            )
        sd = core_api.create_namespaced_secret.call_args.kwargs["body"].string_data
        assert sd["vault-file-0"] == SECRET
        assert sd["GOOGLE_APPLICATION_CREDENTIALS"] == "/var/run/x"
        # The content is not duplicated into the tfvars blob.
        assert SECRET not in sd["terraform.tfvars.json"]


class TestTheSecretFitsAsAWhole:
    async def test_the_whole_secret_is_sized_not_just_the_files(self, fresh_shutdown_event):
        """Kubernetes caps the Secret at 1 MiB across everything in it. Sizing
        only the Vault files let a run pass their 768 KiB cap and then fail at
        creation with a Kubernetes message, instead of one naming the part that
        was actually too big."""
        listener = _make_listener(fresh_shutdown_event)
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            with pytest.raises(ValueError) as exc:
                await listener._create_vars_secret(
                    "tprun-r1-plan-vars",
                    "r1",
                    terraform_vars=[],
                    env_vars=[{"key": "BIG", "value": "x" * (700 * 1024)}],
                    job_name="tprun-r1-plan",
                    job_uid="uid-1",
                    vault_file_values={"vault-file-0": "y" * (400 * 1024)},
                )
        message = str(exc.value)
        assert "Kubernetes Secret limit" in message
        assert "env vars" in message
        core_api.create_namespaced_secret.assert_not_called()
        # Sizes and names only — never any of the content itself.
        assert "xxx" not in message and "yyy" not in message


def _launch_listener(shutdown_event):
    listener = _make_listener(shutdown_event)
    listener._get_runner_token = AsyncMock(return_value="runtok:abc")
    listener._create_auth_secret = AsyncMock()
    listener._create_vars_secret = AsyncMock()
    listener._read_ca_bundle_pem = MagicMock(return_value="")
    listener._report_launch_failed = AsyncMock()
    listener._auth_headers = MagicMock(return_value={})
    listener.runner_config = MagicMock(hooks_enabled=True, runner_namespace="ns")
    return listener


class TestLaunch:
    async def _launch(self, listener, attrs):
        captured = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return {"kind": "Job"}

        with (
            patch("terrapod.runner.job_template.build_job_spec", side_effect=fake_build),
            patch("terrapod.runner.job_manager.create_job", AsyncMock(return_value="job-x")),
            patch("terrapod.runner.job_manager.get_job_uid", AsyncMock(return_value="uid-x")),
            patch("terrapod.runner.listener.arequest_with_retry", new=AsyncMock()),
            patch.object(listener_module, "logger") as log,
        ):
            await listener._launch_run("r1", attrs)
        return captured, log

    async def test_files_alone_create_the_vars_secret_and_reach_the_builder_as_names(
        self, fresh_shutdown_event
    ):
        listener = _launch_listener(fresh_shutdown_event)
        captured, log = await self._launch(
            listener, {"phase": "plan", "vault-files": _files("gcp/adc.json")}
        )
        listener._report_launch_failed.assert_not_called()
        assert captured["vars_secret_name"] == "tprun-r1-plan-vars"
        assert captured["vault_files"] == [{"name": "gcp/adc.json", "secret_key": "vault-file-0"}]
        # The builder never receives a value.
        kwargs = {k: v for k, v in captured.items() if k != "runner_config"}
        assert SECRET not in json.dumps(kwargs, default=str)
        listener._create_vars_secret.assert_awaited_once()
        assert listener._create_vars_secret.await_args.kwargs["vault_file_values"] == {
            "vault-file-0": f"{SECRET}-0"
        }
        assert SECRET not in str(log.mock_calls)

    async def test_an_env_key_clash_reports_a_launch_failure_and_creates_nothing(
        self, fresh_shutdown_event
    ):
        listener = _launch_listener(fresh_shutdown_event)
        create = AsyncMock(return_value="job-x")
        with (
            patch("terrapod.runner.job_manager.create_job", create),
            patch.object(listener_module, "logger") as log,
        ):
            await listener._launch_run(
                "r1",
                {
                    "phase": "plan",
                    "env-vars": [{"key": "vault-file-0", "value": "x"}],
                    "vault-files": _files("a.json"),
                },
            )
        create.assert_not_awaited()
        listener._report_launch_failed.assert_awaited_once()
        run_id, message = listener._report_launch_failed.await_args.args
        assert run_id == "r1"
        assert message.startswith(
            "OpenBao/Vault file delivery refused: env variable 'vault-file-0'"
        )
        assert SECRET not in message
        assert SECRET not in str(log.mock_calls)

    async def test_an_invalid_name_reports_a_launch_failure(self, fresh_shutdown_event):
        listener = _launch_listener(fresh_shutdown_event)
        create = AsyncMock(return_value="job-x")
        with patch("terrapod.runner.job_manager.create_job", create):
            await listener._launch_run(
                "r1", {"phase": "plan", "vault-files": _files("~/.gitconfig")}
            )
        create.assert_not_awaited()
        message = listener._report_launch_failed.await_args.args[1]
        assert "which the runner manages itself" in message
        assert SECRET not in message

    async def test_no_files_means_no_vault_mounts(self, fresh_shutdown_event):
        listener = _launch_listener(fresh_shutdown_event)
        captured, _ = await self._launch(listener, {"phase": "plan"})
        assert captured["vault_files"] == []
        assert captured["vars_secret_name"] == ""
