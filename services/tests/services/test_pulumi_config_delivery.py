"""Pulumi stack config reaches the Job, and never through the Job spec (#1565).

The API resolves a workspace's `pulumi_config` variables and sends them on the
run wire; the listener writes them into the per-run Secret; the Job mounts that
Secret as a file. This pins each hop, because the interesting failures are all
at the joins — a value that rides in the Job spec instead of the Secret, a
listener that reads a key the API never sends, a mount that is not requested so
the file is simply absent and the phase silently sets nothing.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from terrapod.runner import job_template


def _entries() -> list[dict]:
    return [
        {"key": "region", "value": "eu-west-1", "secret": False, "path": False},
        {"key": "dbpass", "value": "sup3rs3cret", "secret": True, "path": False},
        {"key": "outer.inner", "value": "nested", "secret": False, "path": True},
    ]


class TestTheJobSpecCarriesTheMountAndNotTheValues:
    """Same guarantee the tfvars blob has had since variables moved out of the
    spec: `kubectl describe job` must not be a way to read a secret."""

    def _spec(self, **kw):
        from tests.runner.test_job_template import _runner_config

        return job_template.build_job_spec(
            run_id="11111111-1111-1111-1111-111111111111",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-x-plan-auth",
            env_vars=[],
            terraform_vars=[],
            vars_secret_name="tprun-x-plan-vars",
            **kw,
        )

    def test_the_config_file_is_mounted_when_there_is_config(self) -> None:
        spec = self._spec(pulumi_config=_entries())
        volumes = spec["spec"]["template"]["spec"]["volumes"]
        items = [
            i
            for v in volumes
            if v.get("secret", {}).get("secretName") == "tprun-x-plan-vars"
            for i in v["secret"]["items"]
        ]
        assert {"key": "pulumi-config.json", "path": "pulumi-config.json"} in items

    def test_no_config_means_no_mount(self) -> None:
        """A Terraform workspace must not grow a Pulumi-shaped volume item, and
        a Pulumi workspace with no config mounts nothing to read."""
        spec = self._spec(pulumi_config=[])
        rendered = json.dumps(spec, default=str)
        assert "pulumi-config.json" not in rendered

    def test_no_value_appears_anywhere_in_the_rendered_spec(self) -> None:
        spec = self._spec(pulumi_config=_entries())
        rendered = json.dumps(spec, default=str)
        assert "sup3rs3cret" not in rendered
        assert "eu-west-1" not in rendered
        assert "nested" not in rendered


@pytest.mark.asyncio
class TestTheListenerWritesTheConfigIntoTheSecret:
    async def test_the_blob_is_written_with_its_flags_preserved(self) -> None:
        from tests.services.test_listener import _make_listener

        listener = _make_listener(asyncio.Event())
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-plan-vars",
                "r1",
                terraform_vars=[],
                env_vars=[],
                job_name="tprun-r1-plan",
                job_uid="uid-1",
                pulumi_config=_entries(),
            )
        sd = core_api.create_namespaced_secret.call_args.kwargs["body"].string_data
        blob = json.loads(sd["pulumi-config.json"])
        by_key = {e["key"]: e for e in blob}
        assert by_key["dbpass"]["secret"] is True
        assert by_key["dbpass"]["value"] == "sup3rs3cret"
        assert by_key["outer.inner"]["path"] is True
        assert by_key["region"]["secret"] is False and by_key["region"]["path"] is False

    async def test_no_config_writes_no_key(self) -> None:
        from tests.services.test_listener import _make_listener

        listener = _make_listener(asyncio.Event())
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-plan-vars",
                "r1",
                terraform_vars=[{"key": "k", "value": "v", "hcl": False}],
                env_vars=[],
                job_name="tprun-r1-plan",
                job_uid="uid-1",
            )
        sd = core_api.create_namespaced_secret.call_args.kwargs["body"].string_data
        assert "pulumi-config.json" not in sd


class TestConfigAloneIsEnoughToCreateTheSecret:
    """A Pulumi workspace can have config and nothing else — no terraform vars,
    no env vars, no hooks. If that combination does not name a Secret, the whole
    delivery path is skipped and the config silently never arrives.
    """

    def test_the_listener_names_a_vars_secret_for_config_alone(self) -> None:
        import inspect

        from terrapod.runner import listener as listener_mod

        src = inspect.getsource(listener_mod.RunnerListener._launch_run)
        head, _, tail = src.partition("vars_secret_name = (")
        assert tail, "the vars-Secret naming has moved; re-check this guard"
        condition, _, _ = tail.partition(")")
        assert "pulumi_config" in condition
