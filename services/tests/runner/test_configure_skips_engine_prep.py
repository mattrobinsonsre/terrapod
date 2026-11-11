"""A configure prepares no engine (#2508).

A configure-only run used to perform the whole Terraform preparation before the
playbook: fetch and SHA-verify the tofu binary, write the local-backend
override, export a Terrapod provider token, render the workspace's variables to
a tfvars file, and download the current state. It then never invoked an engine.

Two of those are grants rather than waste -- the state can hold secrets and the
token can write this run's state, and both land in the same pod as the
operator's playbook and whatever collections it installs. A third writes the
workspace's variable values, sensitive ones included, into a directory the
playbook can read.

Every assertion here is paired with the same assertion on a PLAN, because the
property is a *difference* between the phases: a test that only drove a
configure would pass if the step had been removed for every phase, which is the
opposite defect and a worse one.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from terrapod.runner import job_entrypoint
from terrapod.runner.runner_config import RunnerConfig

_PREP = (
    "download_binary",
    "download_configuration",
    "download_state",
)


def _cfg(phase: str) -> RunnerConfig:
    """A workspace that genuinely has BOTH halves.

    `TP_WORKING_DIR` is set so the Terraform half exists and the comparison is
    against a run that really would need the preparation -- which is the case
    the issue reported, rather than a configure-only workspace where every step
    would be vacuous.
    """
    return RunnerConfig.from_env(
        env={
            "TP_API_URL": "http://api.invalid",
            "TP_RUN_ID": "run-1",
            "TP_BACKEND": "tofu",
            "TP_VERSION": "1.12.1",
            "TP_PHASE": phase,
            "TP_WORKING_DIR": "infra",
            "TP_CONFIG_DIRECTORY": "ansible",
            "TP_AUTH_TOKEN": "runtok:run-1:configure:1:1:sig",
        }
    )


class _Calls:
    """What the preparation actually did, for one invocation of `_run_body`."""

    def __init__(self) -> None:
        self.binary = 0
        self.override_written: list[bool] = []
        self.provider_env = 0
        self.tfvars = 0
        self.state = 0
        self.mirror_rc = 0


def _drive(tmp_path: Path, phase: str) -> _Calls:
    calls = _Calls()
    cfg = _cfg(phase)
    tmp_path.mkdir(parents=True, exist_ok=True)

    def fake_download_configuration(_cfg, *, work_dir, client=None, write_backend_override=True):
        calls.override_written.append(write_backend_override)
        result = MagicMock()
        result.strip_dir = work_dir
        return result

    def fake_binary(_cfg):
        calls.binary += 1
        return tmp_path / "bin" / "tofu"

    def fake_provider_env(_cfg, *, env, engine):
        calls.provider_env += 1
        return {"TERRAPOD_TOKEN": "t", "TERRAPOD_HOSTNAME": "h"}

    def fake_state(_cfg, *, strip_dir):
        calls.state += 1
        return False

    def fake_rc(**_kw):
        calls.mirror_rc += 1

    def fake_tfvars(*_a, **_kw):
        calls.tfvars += 1

    # The tfvars step is keyed on the mounted Secret EXISTING, so the file has
    # to be there or the step is skipped for a reason unrelated to the phase --
    # which would make the configure assertion pass vacuously.
    vars_file = tmp_path / "vars.json"
    vars_file.write_text('{"a": "b"}')

    with (
        patch.object(job_entrypoint, "download_binary", side_effect=fake_binary),
        patch.object(
            job_entrypoint, "download_configuration", side_effect=fake_download_configuration
        ),
        patch.object(job_entrypoint, "download_state", side_effect=fake_state),
        patch.object(job_entrypoint.mirror_config, "write_terraform_rc", side_effect=fake_rc),
        patch.object(job_entrypoint.mirror_config, "export_env", return_value={}),
        patch.object(
            job_entrypoint.provider_credentials, "export_env", side_effect=fake_provider_env
        ),
        patch.object(job_entrypoint.tfvars, "write_auto_tfvars", side_effect=fake_tfvars),
        patch.object(job_entrypoint.working_dir, "resolve_and_chdir", return_value=tmp_path),
        patch.object(job_entrypoint.execution_hooks, "run_point", return_value=None),
        patch.object(job_entrypoint, "_VARS_FILE", tmp_path / "vars.json"),
        # Both terminal branches return immediately, so nothing below step 7
        # runs and this exercises the preparation alone.
        patch.object(job_entrypoint, "_run_configure_phase", return_value=0),
        patch.object(job_entrypoint, "_run_plan_phase", return_value=0),
        patch.object(job_entrypoint, "init_phase"),
        patch.object(job_entrypoint, "backend_backstop"),
        # Step 10b on the PLAN leg mints cloud-identity tokens over the network.
        # Left unpatched it reaches real DNS, which fails on a CI runner and
        # succeeds-or-hangs on a workstation -- so the control would be
        # measuring the machine rather than the phase.
        patch.object(job_entrypoint.cloud_identity, "run", return_value={}),
    ):
        job_entrypoint._run_body(cfg, tmp_path)
    return calls


def test_a_configure_fetches_no_engine_binary(tmp_path):
    assert _drive(tmp_path / "c", "configure").binary == 0
    assert _drive(tmp_path / "p", "plan").binary == 1


def test_a_configure_writes_no_backend_override(tmp_path):
    # The tarball is still fetched -- the playbook and inventory come from it --
    # so this asserts the ARGUMENT, not that the step was skipped.
    assert _drive(tmp_path / "c", "configure").override_written == [False]
    assert _drive(tmp_path / "p", "plan").override_written == [True]


def test_a_configure_exports_no_provider_token(tmp_path):
    """The security half. The token reaches every provider plugin the
    configuration loads, and a configure loads none."""
    assert _drive(tmp_path / "c", "configure").provider_env == 0
    assert _drive(tmp_path / "p", "plan").provider_env == 1


def test_a_configure_downloads_no_state(tmp_path):
    """The other security half: state can hold secrets, and nothing in a
    configure reads a state file."""
    assert _drive(tmp_path / "c", "configure").state == 0
    assert _drive(tmp_path / "p", "plan").state == 1


def test_a_configure_renders_no_tfvars(tmp_path):
    """Pure disclosure for this phase: the file is Terraform's delivery
    mechanism and a configure has its own, so writing it only drops the
    workspace's values -- sensitive ones included -- where the playbook can
    read them. The same argument step 4b already makes for Pulumi."""
    assert _drive(tmp_path / "c", "configure").tfvars == 0
    assert _drive(tmp_path / "p", "plan").tfvars == 1


def test_a_configure_writes_no_terraform_cli_config(tmp_path):
    assert _drive(tmp_path / "c", "configure").mirror_rc == 0
    assert _drive(tmp_path / "p", "plan").mirror_rc == 1


@pytest.mark.parametrize("name", _PREP)
def test_the_gate_is_the_phase_and_not_the_engine(name, tmp_path):
    """`is_configure` must read `cfg.phase`.

    Keyed on the phase rather than on the run's shape because a composite is two
    runner Jobs driven by one Run: the configure Job of a composite is as free of
    Terraform as a standalone one, and its apply Job is a separate Job that still
    needs everything.
    """
    import inspect

    src = inspect.getsource(job_entrypoint._run_body)
    assert 'is_configure = cfg.phase == "configure"' in src
    assert name in src
