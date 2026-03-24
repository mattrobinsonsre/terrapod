"""Tests for runner job_template — var_files injection."""

import json
from unittest.mock import MagicMock


def _runner_config():
    """Create a minimal RunnerConfig mock."""
    cfg = MagicMock()
    cfg.image.repository = "ghcr.io/test/runner"
    cfg.image.tag = "latest"
    cfg.image.pull_policy = "IfNotPresent"
    cfg.default = "default"
    cfg.default_terraform_version = "1.11"
    cfg.default_execution_backend = "tofu"
    cfg.ttl_seconds_after_finished = 300
    cfg.azure_workload_identity = False
    cfg.node_selector = {}
    cfg.tolerations = []
    cfg.affinity = {}
    cfg.priority_class_name = ""
    cfg.topology_spread_constraints = []
    cfg.pod_security_context = {}
    cfg.pod_annotations = {}

    default_def = MagicMock()
    default_def.name = "default"
    default_def.setup_script = ""
    cfg.definitions = [default_def]

    return cfg


class TestVarFilesInjection:
    def test_var_files_env_var_set(self):
        """TP_VAR_FILES should be set when var_files is provided."""
        from terrapod.runner.job_template import build_job_spec

        spec = build_job_spec(
            run_id="abc123",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
            var_files=["envs/dev.tfvars", "secrets.tfvars"],
        )

        container = spec["spec"]["template"]["spec"]["containers"][0]
        env_dict = {e["name"]: e.get("value") for e in container["env"] if "value" in e}
        assert "TP_VAR_FILES" in env_dict
        parsed = json.loads(env_dict["TP_VAR_FILES"])
        assert parsed == ["envs/dev.tfvars", "secrets.tfvars"]

    def test_no_var_files_env_var_when_empty(self):
        """TP_VAR_FILES should NOT be set when var_files is empty."""
        from terrapod.runner.job_template import build_job_spec

        spec = build_job_spec(
            run_id="abc123",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
            var_files=[],
        )

        container = spec["spec"]["template"]["spec"]["containers"][0]
        env_names = {e["name"] for e in container["env"]}
        assert "TP_VAR_FILES" not in env_names

    def test_no_var_files_env_var_when_none(self):
        """TP_VAR_FILES should NOT be set when var_files is None."""
        from terrapod.runner.job_template import build_job_spec

        spec = build_job_spec(
            run_id="abc123",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
            var_files=None,
        )

        container = spec["spec"]["template"]["spec"]["containers"][0]
        env_names = {e["name"] for e in container["env"]}
        assert "TP_VAR_FILES" not in env_names

    def test_var_files_default_omitted(self):
        """TP_VAR_FILES should NOT be set when var_files is not passed."""
        from terrapod.runner.job_template import build_job_spec

        spec = build_job_spec(
            run_id="abc123",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
        )

        container = spec["spec"]["template"]["spec"]["containers"][0]
        env_names = {e["name"] for e in container["env"]}
        assert "TP_VAR_FILES" not in env_names

    def test_auth_token_from_secret_ref(self):
        """TP_AUTH_TOKEN should use secretKeyRef, not a plain value."""
        from terrapod.runner.job_template import build_job_spec

        spec = build_job_spec(
            run_id="abc123",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
        )

        container = spec["spec"]["template"]["spec"]["containers"][0]
        auth_env = next(e for e in container["env"] if e["name"] == "TP_AUTH_TOKEN")
        assert "valueFrom" in auth_env
        assert auth_env["valueFrom"]["secretKeyRef"]["name"] == "tprun-abc12345-auth"
        assert auth_env["valueFrom"]["secretKeyRef"]["key"] == "token"
