"""Platform plumbing cannot be shadowed by a workspace variable (GHSA-7859-pwwx-vx4f).

The Job's container env is built platform-block first, workspace variables
second, and Kubernetes gives the LAST duplicate precedence — so a workspace
variable named `TP_AUTH_TOKEN` won. Anyone with variable-write could point the
runner at a host they control and collect the run's own token.
"""

from terrapod.runner.reserved_env import is_reserved_env_key


class TestTheReservedPredicate:
    def test_platform_plumbing_is_reserved(self):
        for key in ("TP_API_URL", "TP_AUTH_TOKEN", "TP_RUN_ID", "TP_DESTROY", "TP_VERIFY_BINARIES"):
            assert is_reserved_env_key(key), key

    def test_case_and_whitespace_do_not_evade_it(self):
        for key in ("tp_auth_token", "Tp_Api_Url", "  TP_AUTH_TOKEN"):
            assert is_reserved_env_key(key), key

    def test_terraform_settings_are_NOT_reserved(self):
        # Deliberate: HCP Terraform and TFE document setting TF_LOG as a
        # workspace environment variable as the supported way to debug a run.
        # Refusing it would break a migrating user following HashiCorp's own
        # runbook, to enforce a policy the incumbent does not have.
        for key in ("TF_LOG", "TF_LOG_PROVIDER", "TF_LOG_PATH", "TF_VAR_region", "AWS_REGION"):
            assert not is_reserved_env_key(key), key


class TestTheJobSpecDropsThem:
    def _build(self, env_vars):
        from terrapod.engines.terraform import TerraformRunOptions, TerraformStrategy
        from tests.runner.test_job_template import _runner_config

        cfg = _runner_config()
        return TerraformStrategy().build_job_spec(
            options=TerraformRunOptions(),
            run_id="abc123",
            phase="plan",
            runner_config=cfg,
            auth_secret_name="tprun-abc12345-auth",
            env_vars=env_vars,
            terraform_vars=[],
        )

    def test_a_shadowing_variable_never_reaches_the_container_env(self):
        """The behavioural assertion: the platform value is the one that survives."""
        spec = self._build(
            [
                {"key": "TP_API_URL", "value": "http://attacker.example"},
                {"key": "TP_AUTH_TOKEN", "value": "stolen"},
                {"key": "AWS_REGION", "value": "eu-west-1"},
            ]
        )
        container = spec["spec"]["template"]["spec"]["containers"][0]
        names = [e["name"] for e in container["env"]]
        values = {e["name"]: e.get("value") for e in container["env"] if "value" in e}

        # The attacker's values are gone entirely...
        assert "http://attacker.example" not in values.values()
        assert "stolen" not in values.values()
        # ...the platform still sets its own, exactly once...
        assert names.count("TP_API_URL") == 1
        assert values.get("TP_API_URL") == "http://terrapod-api:8000"
        # ...and an ordinary variable is untouched.
        assert values.get("AWS_REGION") == "eu-west-1"

    def test_the_predicate_is_shared_not_copied(self):
        from terrapod.runner.job_template import is_reserved_env_key as imported

        assert imported is is_reserved_env_key
