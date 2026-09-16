"""Vault file delivery reaches every engine's runner Job (#1619).

A file-mode Vault variable's value is the file's path, whatever the engine. An
engine whose Job did not mount the files would hand its program a path to a file
that does not exist: a Pulumi program reading `GOOGLE_APPLICATION_CREDENTIALS`
would fail on a missing file, or fall back to whatever other credentials the
pod can find.

So the mounts live in the one engine-neutral builder
(`runner/job_template.build_job_spec`) that every strategy composes, and these
tests pin that for **every registered engine** (gated or not), read from the
registry rather than listed by hand, so an engine added later is covered the
moment it is registered.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import terrapod.runner.listener as listener_module
from terrapod import engines
from terrapod.config import settings
from tests.runner.test_engine_options_seam import ATTRS, _runner_config
from tests.services.test_listener import _make_listener

SECRET = "S3CR3T-vault-file-content-must-not-reach-any-job-spec"
VARS = "tprun-01a0871a3dd773a8-plan-vars"

#: Every engine this build contains, gated or not. A new strategy lands here.
ENGINES = sorted(engines._REGISTRY)

#: What the listener hands the builder: names and Secret keys, never values.
MOUNTS = [
    {"name": "gcp/adc.json", "secret_key": "vault-file-0"},
    {"name": "~/.aws/credentials", "secret_key": "vault-file-1"},
]

#: The variables whose values are those files' paths, as the API sends them.
ENV_VARS = [
    {"key": "GOOGLE_APPLICATION_CREDENTIALS", "value": "/var/run/terrapod/files/gcp/adc.json"},
    {"key": "AWS_SHARED_CREDENTIALS_FILE", "value": "/home/runner/.aws/credentials"},
]


def test_the_table_covers_every_engine_this_line_runs():
    """Guards the parametrisation below from silently shrinking."""
    assert {"terraform", "pulumi"} <= set(ENGINES)


def _as_the_listener_calls_it(engine: str, vault_files: list[dict]) -> dict:
    """The listener's exact kwargs (see test_engine_options_seam), plus files."""
    s = engines._REGISTRY[engine]
    return s.build_job_spec(
        options=s.options_from_attrs(ATTRS, "plan"),
        run_id="01a0871a3dd773a8",
        phase="plan",
        runner_config=_runner_config(),
        auth_secret_name="tprun-01a0871a3dd773a8-plan-auth",
        vars_secret_name=VARS,
        env_vars=ENV_VARS,
        terraform_vars=[],
        execution_hooks=[],
        git_auth=[],
        vault_files=vault_files,
        resource_cpu="2",
        resource_memory="4Gi",
        ca_secret_name="",
    )


def _vault_parts(spec: dict) -> dict:
    """Everything in a pod spec that file delivery put there."""
    pod = spec["spec"]["template"]["spec"]
    container = pod["containers"][0]
    return {
        "volumes": [v for v in pod["volumes"] if v["name"].startswith("vault-")],
        "mounts": [m for m in container["volumeMounts"] if m["name"].startswith("vault-")],
        "init": [c for c in pod.get("initContainers", []) if c["name"] == "home-dirs"],
    }


@pytest.mark.parametrize("engine", ENGINES)
class TestEveryEngineMountsTheFiles:
    def test_relative_and_home_files_are_mounted_read_only(self, engine):
        parts = _vault_parts(_as_the_listener_calls_it(engine, MOUNTS))
        assert parts["volumes"] == [
            {
                "name": "vault-files",
                "secret": {
                    "secretName": VARS,
                    "items": [{"key": "vault-file-0", "path": "gcp/adc.json"}],
                    "defaultMode": 0o444,
                },
            },
            {
                "name": "vault-home-files",
                "secret": {
                    "secretName": VARS,
                    "items": [{"key": "vault-file-1", "path": "vault-file-1"}],
                    "defaultMode": 0o444,
                },
            },
        ]
        assert parts["mounts"] == [
            {"name": "vault-files", "mountPath": "/var/run/terrapod/files", "readOnly": True},
            {
                "name": "vault-home-files",
                "mountPath": "/home/runner/.aws/credentials",
                "subPath": "vault-file-1",
                "readOnly": True,
            },
        ]
        (init,) = parts["init"]
        assert init["command"] == ["mkdir", "-p", "/home/runner/.aws"]
        assert init["securityContext"]["runAsUser"] == 1000

    def test_the_path_variables_come_from_the_secret_not_the_spec(self, engine):
        """The variable carries the path; like every env value it is a
        secretKeyRef, so neither a path nor a secret is literal in the spec."""
        spec = _as_the_listener_calls_it(engine, MOUNTS)
        env = {e["name"]: e for e in spec["spec"]["template"]["spec"]["containers"][0]["env"]}
        for var in ENV_VARS:
            assert env[var["key"]] == {
                "name": var["key"],
                "valueFrom": {"secretKeyRef": {"name": VARS, "key": var["key"]}},
            }
        assert SECRET not in json.dumps(spec, default=str)

    def test_no_value_reaches_the_spec_even_if_an_entry_carries_one(self, engine):
        leaky = [dict(m, value=SECRET) for m in MOUNTS]
        assert SECRET not in json.dumps(_as_the_listener_calls_it(engine, leaky), default=str)

    def test_the_mounts_are_identical_to_terraforms(self, engine):
        """One implementation, so one result: an engine that rendered the files
        differently would be a second, untested delivery path."""
        assert _vault_parts(_as_the_listener_calls_it(engine, MOUNTS)) == _vault_parts(
            _as_the_listener_calls_it("terraform", MOUNTS)
        )

    def test_without_files_nothing_is_added(self, engine):
        assert _vault_parts(_as_the_listener_calls_it(engine, [])) == {
            "volumes": [],
            "mounts": [],
            "init": [],
        }

    def test_the_strategy_forwards_vault_files_to_the_shared_builder(self, engine):
        """The load-bearing property. A strategy that built its own spec, or
        passed the builder an explicit subset of kwargs, would drop the files
        without failing anything above for Terraform."""
        captured: dict = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return {}

        sentinel = [{"name": "x", "secret_key": "vault-file-0"}]
        with patch("terrapod.runner.job_template.build_job_spec", side_effect=fake_build):
            _as_the_listener_calls_it(engine, sentinel)
        assert captured["vault_files"] is sentinel
        assert captured["vars_secret_name"] == VARS


@pytest.fixture
def fresh_shutdown_event():
    import asyncio

    new_event = asyncio.Event()
    old_event = listener_module._shutdown
    listener_module._shutdown = new_event
    yield new_event
    listener_module._shutdown = old_event


@pytest.fixture
def every_engine_enabled(monkeypatch):
    """The listener resolves through the gate; this is about delivery, not gating."""
    for name in ENGINES:
        cfg = getattr(settings.engines, name, None)
        if cfg is not None:
            monkeypatch.setattr(cfg, "enabled", True)


@pytest.mark.usefixtures("every_engine_enabled")
@pytest.mark.parametrize("engine", ENGINES)
async def test_the_listener_launches_every_engine_with_the_files(engine, fresh_shutdown_event):
    """Through `_launch_run` with the real strategy and the real builder: the
    Job handed to Kubernetes mounts the files, and their content goes only to
    the vars Secret."""
    listener = _make_listener(fresh_shutdown_event)
    listener._get_runner_token = AsyncMock(return_value="runtok:abc")
    listener._create_auth_secret = AsyncMock()
    listener._create_vars_secret = AsyncMock()
    listener._read_ca_bundle_pem = MagicMock(return_value="")
    listener._report_launch_failed = AsyncMock()
    listener._auth_headers = MagicMock(return_value={})
    cfg = _runner_config()
    cfg.hooks_enabled = True
    listener.runner_config = cfg

    attrs = dict(
        ATTRS,
        phase="plan",
        engine=engine,
        **{
            "env-vars": ENV_VARS,
            "vault-files": [
                {"key": "GOOGLE_APPLICATION_CREDENTIALS", "name": "gcp/adc.json", "value": SECRET},
                {
                    "key": "AWS_SHARED_CREDENTIALS_FILE",
                    "name": "~/.aws/credentials",
                    "value": SECRET + "-aws",
                },
            ],
        },
    )
    create = AsyncMock(return_value="tprun-r1-plan")
    with (
        patch("terrapod.runner.job_manager.create_job", create),
        patch("terrapod.runner.job_manager.get_job_uid", AsyncMock(return_value="uid-x")),
        patch("terrapod.runner.listener.arequest_with_retry", new=AsyncMock()),
        patch.object(listener_module, "logger") as log,
    ):
        await listener._launch_run("01a0871a3dd773a8aaaa", attrs)

    listener._report_launch_failed.assert_not_called()
    create.assert_awaited_once()
    spec = create.await_args.args[0]
    assert _vault_parts(spec) == _vault_parts(_as_the_listener_calls_it("terraform", MOUNTS))
    env = {
        e["name"]: e.get("value") for e in spec["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    if engine != "terraform":
        assert env["TP_ENGINE"] == engine
    assert SECRET not in json.dumps(spec, default=str)
    assert listener._create_vars_secret.await_args.kwargs["vault_file_values"] == {
        "vault-file-0": SECRET,
        "vault-file-1": SECRET + "-aws",
    }
    assert SECRET not in str(log.mock_calls)
