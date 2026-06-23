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


class TestPodFailurePolicy:
    """Phase-conditional pod failure policy.

    plan jobs: K8s-native retry on disruption (eviction, preemption, drain)
               because plan is read-only on AWS — partial execution is safe
               to retry. Rule order matters: Ignore-on-DisruptionTarget MUST
               come BEFORE FailJob-on-non-zero, otherwise the SIGKILL-derived
               exit 137 from eviction would fail the Job before the disruption
               rule could ignore it.

    apply jobs: never retry on disruption. Once the container has run, it may
                have mutated state (resources created/modified/destroyed);
                K8s retry would risk double-create or split-brain. Operator
                decides whether to re-run from the Run UI.
    """

    def _build(self, phase: str):
        from terrapod.runner.job_template import build_job_spec

        return build_job_spec(
            run_id="abc123",
            phase=phase,
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
        )

    def test_backoff_limit_unchanged(self):
        for phase in ("plan", "apply"):
            spec = self._build(phase)
            assert spec["spec"]["backoffLimit"] == 3

    def test_plan_ignores_disruption_first(self):
        """Plan rule[0] must Ignore on DisruptionTarget=True."""
        rules = self._build("plan")["spec"]["podFailurePolicy"]["rules"]
        assert rules[0] == {
            "action": "Ignore",
            "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}],
        }

    def test_plan_then_fails_on_nonzero_exit(self):
        """Plan rule[1] is FailJob on non-zero exit."""
        rules = self._build("plan")["spec"]["podFailurePolicy"]["rules"]
        assert len(rules) == 2
        assert rules[1]["action"] == "FailJob"
        assert rules[1]["onExitCodes"] == {
            "containerName": "runner",
            "operator": "NotIn",
            "values": [0],
        }

    def test_apply_only_fails_on_nonzero_exit(self):
        """Apply has no Ignore rule — DisruptionTarget falls through to FailJob via exit-137."""
        rules = self._build("apply")["spec"]["podFailurePolicy"]["rules"]
        assert len(rules) == 1
        assert rules[0]["action"] == "FailJob"
        assert rules[0]["onExitCodes"] == {
            "containerName": "runner",
            "operator": "NotIn",
            "values": [0],
        }
        # Critically: NO Ignore-on-DisruptionTarget rule. An eviction's
        # exit-137 will hit the FailJob rule and the run will error.
        assert not any(r["action"] == "Ignore" for r in rules)


class TestAuthTokenInjection:
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

    def test_home_env_set_to_home_runner(self):
        """HOME must be present in the Job's env so tools that consult
        $HOME (helm's cache, kubectl's cache, AWS CLI, git) don't fall
        back to "/" — the misleading "looks like X is not a valid chart
        repository" error from terraform-provider-helm traces back to
        an unset HOME and Helm writing to /.cache/...
        """
        from terrapod.runner.job_template import build_job_spec

        spec = build_job_spec(
            run_id="abc123",
            phase="apply",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
        )
        container = spec["spec"]["template"]["spec"]["containers"][0]
        home_env = next((e for e in container["env"] if e["name"] == "HOME"), None)
        assert home_env is not None, "HOME env var missing from runner Job spec"
        assert home_env["value"] == "/home/runner"


class TestPublicApiUrl:
    """Split-networking: TP_PUBLIC_API_URL only emitted when distinct from TP_API_URL."""

    def _build(self, monkeypatch, api_url, public_api_url):
        from terrapod.runner.job_template import build_job_spec

        monkeypatch.setenv("TERRAPOD_API_URL", api_url)
        if public_api_url is None:
            monkeypatch.delenv("TERRAPOD_PUBLIC_API_URL", raising=False)
        else:
            monkeypatch.setenv("TERRAPOD_PUBLIC_API_URL", public_api_url)
        spec = build_job_spec(
            run_id="abc123",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc12345-auth",
            env_vars=[],
            terraform_vars=[],
        )
        return spec["spec"]["template"]["spec"]["containers"][0]["env"]

    def test_public_api_url_emitted_when_different(self, monkeypatch):
        env = self._build(
            monkeypatch,
            api_url="https://terrapod-internal.example.com",
            public_api_url="https://terrapod.example.com",
        )
        env_dict = {e["name"]: e.get("value") for e in env if "value" in e}
        assert env_dict["TP_API_URL"] == "https://terrapod-internal.example.com"
        assert env_dict["TP_PUBLIC_API_URL"] == "https://terrapod.example.com"

    def test_public_api_url_omitted_when_same(self, monkeypatch):
        env = self._build(
            monkeypatch,
            api_url="https://terrapod.example.com",
            public_api_url="https://terrapod.example.com",
        )
        env_names = {e["name"] for e in env}
        assert "TP_API_URL" in env_names
        assert "TP_PUBLIC_API_URL" not in env_names

    def test_public_api_url_omitted_when_unset(self, monkeypatch):
        env = self._build(
            monkeypatch,
            api_url="https://terrapod.example.com",
            public_api_url=None,
        )
        env_names = {e["name"] for e in env}
        assert "TP_PUBLIC_API_URL" not in env_names

    def test_public_api_url_omitted_when_empty_string(self, monkeypatch):
        # The Helm template renders "" when neither listener.publicApiUrl
        # nor api.config.external_url is set — must be treated as "unset".
        env = self._build(
            monkeypatch,
            api_url="https://terrapod.example.com",
            public_api_url="",
        )
        env_names = {e["name"] for e in env}
        assert "TP_PUBLIC_API_URL" not in env_names

    def test_public_api_url_omitted_when_trailing_slash_only_difference(self, monkeypatch):
        # Operators may set publicApiUrl with a trailing slash while
        # apiUrl has none (or vice versa). That's not a real difference;
        # the runner entrypoint's host extraction would discard them too.
        # Verifies the .rstrip("/") guard in build_job_spec.
        env = self._build(
            monkeypatch,
            api_url="https://terrapod.example.com",
            public_api_url="https://terrapod.example.com/",
        )
        env_names = {e["name"] for e in env}
        assert "TP_PUBLIC_API_URL" not in env_names


class TestVarsSecretDelivery:
    """Workspace variable values are delivered via the per-run vars Secret —
    terraform vars as a mounted tfvars file, env vars via secretKeyRef — so
    NOTHING is plaintext in the Job spec (the security contract)."""

    def _spec(self, **kw):
        from terrapod.runner.job_template import build_job_spec

        return build_job_spec(
            run_id="abc123def456",
            phase="plan",
            runner_config=_runner_config(),
            auth_secret_name="tprun-abc123def456-plan-auth",
            **kw,
        )

    def test_terraform_var_mounts_tfvars_volume_no_plaintext(self):
        spec = self._spec(
            env_vars=[],
            terraform_vars=[{"key": "secret", "value": "s3cr3t", "hcl": False}],
            vars_secret_name="tprun-abc123def456-plan-vars",
        )
        pod = spec["spec"]["template"]["spec"]
        # Volume + mount present, referencing the vars Secret's tfvars key.
        vol = next(v for v in pod["volumes"] if v["name"] == "tfvars")
        assert vol["secret"]["secretName"] == "tprun-abc123def456-plan-vars"
        assert vol["secret"]["items"][0]["key"] == "terraform.tfvars.json"
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "tfvars")
        assert mount["readOnly"] is True
        # The secret value never appears as a plaintext env value, and no
        # TF_VAR_ env is emitted.
        env = pod["containers"][0]["env"]
        assert not any(e["name"].startswith("TF_VAR_") for e in env)
        assert not any(e.get("value") == "s3cr3t" for e in env)

    def test_env_var_via_secret_key_ref_not_plaintext(self):
        spec = self._spec(
            env_vars=[{"key": "AWS_SECRET_ACCESS_KEY", "value": "AKIAsecret"}],
            terraform_vars=[],
            vars_secret_name="tprun-abc123def456-plan-vars",
        )
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        aws = next(e for e in env if e["name"] == "AWS_SECRET_ACCESS_KEY")
        # secretKeyRef into the vars Secret, NOT a plaintext value.
        assert "value" not in aws
        assert aws["valueFrom"]["secretKeyRef"]["name"] == "tprun-abc123def456-plan-vars"
        assert aws["valueFrom"]["secretKeyRef"]["key"] == "AWS_SECRET_ACCESS_KEY"
        # The plaintext value appears nowhere in the spec.
        assert "AKIAsecret" not in str(spec)

    def test_no_volume_when_no_terraform_vars(self):
        spec = self._spec(
            env_vars=[{"key": "FOO", "value": "bar"}],
            terraform_vars=[],
            vars_secret_name="tprun-abc123def456-plan-vars",
        )
        pod = spec["spec"]["template"]["spec"]
        assert not any(v["name"] == "tfvars" for v in pod["volumes"])
