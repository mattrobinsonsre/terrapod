"""The engine's parameter channel reaches the Job, never through the Job spec.

A workspace's **native** variables are one list for every engine (#1898): the
API resolves them, the listener writes them into the per-run Secret as one blob,
the Job mounts that blob as one file, and the entrypoint dispatches on the run's
engine — a tfvars file for Terraform, `pulumi config set` for Pulumi.

This pins each hop, because the interesting failures are all at the joins: a
value that rides in the Job spec instead of the Secret, a listener that reads a
key the API never sends, a mount that is not requested so the file is simply
absent and the delivery silently does nothing.

The flags are the reason one list can serve every engine. Each entry carries
`structured` and `sensitive`; a delivery uses what it can honour and ignores the
rest. Terraform ignores `sensitive` because there the file *is* the mechanism
and every value is written the same way; Pulumi turns it into `--secret`, which
makes its own engine render `[secret]` in the preview and the state.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from terrapod.runner import job_template


def _entries() -> list[dict]:
    return [
        {"key": "region", "value": "eu-west-1", "structured": False, "sensitive": False},
        {"key": "dbpass", "value": "sup3rs3cret", "structured": False, "sensitive": True},
        {"key": "outer.inner", "value": "nested", "structured": True, "sensitive": False},
    ]


class TestTheJobSpecCarriesTheMountAndNotTheValues:
    """Same guarantee the blob has had since variables moved out of the spec:
    `kubectl describe job` must not be a way to read a secret."""

    def _spec(self, **kw):
        from tests.runner.test_job_template import _runner_config

        return job_template.build_job_spec(
            run_id="11111111-1111-1111-1111-111111111111",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-x-plan-auth",
            env_vars=[],
            vars_secret_name="tprun-x-plan-vars",
            **kw,
        )

    def _items(self, spec) -> list[dict]:
        return [
            i
            for v in spec["spec"]["template"]["spec"]["volumes"]
            if v.get("secret", {}).get("secretName") == "tprun-x-plan-vars"
            for i in v["secret"]["items"]
        ]

    def test_the_blob_is_mounted_when_there_are_variables(self) -> None:
        spec = self._spec(terraform_vars=_entries())
        assert {"key": "terraform.tfvars.json", "path": "terraform.tfvars.json"} in self._items(
            spec
        )

    def test_there_is_exactly_one_variable_mount(self) -> None:
        """#1565 mounted a second, Pulumi-only file carrying the same four
        fields under other names. Collapsing it is the point of #1898, and a
        second item reappearing is how that would silently come back."""
        spec = self._spec(terraform_vars=_entries())
        keys = [i["key"] for i in self._items(spec)]
        assert keys == ["terraform.tfvars.json"]

    def test_no_variables_means_no_mount(self) -> None:
        spec = self._spec(terraform_vars=[])
        assert "terraform.tfvars.json" not in json.dumps(spec, default=str)

    def test_no_value_appears_anywhere_in_the_rendered_spec(self) -> None:
        spec = self._spec(terraform_vars=_entries())
        rendered = json.dumps(spec, default=str)
        assert "sup3rs3cret" not in rendered
        assert "eu-west-1" not in rendered
        assert "nested" not in rendered


@pytest.mark.asyncio
class TestTheListenerWritesOneBlobWithBothFlags:
    async def _written(self, **kw) -> dict:
        from tests.services.test_listener import _make_listener

        listener = _make_listener(asyncio.Event())
        core_api = MagicMock()
        with patch("terrapod.runner.job_manager._get_core_api", return_value=core_api):
            await listener._create_vars_secret(
                "tprun-r1-plan-vars",
                "r1",
                env_vars=[],
                job_name="tprun-r1-plan",
                job_uid="uid-1",
                **kw,
            )
        return core_api.create_namespaced_secret.call_args.kwargs["body"].string_data

    async def test_the_flags_and_values_survive_the_hop(self) -> None:
        sd = await self._written(terraform_vars=_entries())
        blob = {e["key"]: e for e in json.loads(sd["terraform.tfvars.json"])}
        assert blob["dbpass"]["sensitive"] is True
        assert blob["dbpass"]["value"] == "sup3rs3cret"
        assert blob["outer.inner"]["structured"] is True
        assert blob["region"]["sensitive"] is False
        assert blob["region"]["structured"] is False

    async def test_both_spellings_of_structured_are_written(self) -> None:
        """`hcl` is `structured`'s permanent wire twin (#1435), and a runner up
        to N-2 minors behind reads the old one."""
        sd = await self._written(terraform_vars=[{"key": "k", "value": "v", "structured": True}])
        entry = json.loads(sd["terraform.tfvars.json"])[0]
        assert entry["structured"] is True and entry["hcl"] is True

    async def test_a_lagging_api_resolves_sensitive_to_false(self) -> None:
        """An API that predates #1898 sends no `sensitive`. That must read as
        false — what every run did before — rather than raising."""
        sd = await self._written(terraform_vars=[{"key": "k", "value": "v", "hcl": False}])
        assert json.loads(sd["terraform.tfvars.json"])[0]["sensitive"] is False

    async def test_there_is_no_second_variable_key(self) -> None:
        sd = await self._written(terraform_vars=_entries())
        assert "pulumi-config.json" not in sd


class TestVariablesAloneAreEnoughToCreateTheSecret:
    """A workspace can have native variables and nothing else — no env vars, no
    hooks, no git auth. If that combination does not name a Secret, the whole
    delivery path is skipped and the variables silently never arrive. That is
    true for a Pulumi workspace exactly as it is for a Terraform one, which is
    what makes one list the right shape.
    """

    def test_the_listener_names_a_vars_secret_for_variables_alone(self) -> None:
        import inspect

        from terrapod.runner import listener as listener_mod

        src = inspect.getsource(listener_mod.RunnerListener._launch_run)
        head, _, tail = src.partition("vars_secret_name = (")
        assert tail, "the vars-Secret naming has moved; re-check this guard"
        condition, _, _ = tail.partition(")")
        assert "native_vars" in condition
