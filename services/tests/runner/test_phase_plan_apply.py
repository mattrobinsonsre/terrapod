"""Tests for terrapod.runner.phases.plan_apply."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from terrapod.runner.phases import plan_apply
from terrapod.runner.runner_config import RunnerConfig


def _cfg(**overrides) -> RunnerConfig:
    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
    }
    base.update(overrides)
    return RunnerConfig.from_env(env=base)


class _Result:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class TestBuildPlanArgv:
    def test_minimal(self) -> None:
        argv = plan_apply.build_plan_argv(_cfg(), binary="tofu", var_file_args=[])
        assert argv == ["tofu", "plan", "-input=false", "-detailed-exitcode", "-out=tfplan"]

    def test_refresh_only(self) -> None:
        argv = plan_apply.build_plan_argv(
            _cfg(TP_REFRESH_ONLY="true"), binary="tofu", var_file_args=[]
        )
        assert "-refresh-only" in argv

    def test_refresh_false(self) -> None:
        argv = plan_apply.build_plan_argv(_cfg(TP_REFRESH="false"), binary="tofu", var_file_args=[])
        assert "-refresh=false" in argv

    def test_destroy(self) -> None:
        argv = plan_apply.build_plan_argv(_cfg(TP_DESTROY="true"), binary="tofu", var_file_args=[])
        assert "-destroy" in argv

    def test_var_files_targets_replaces(self) -> None:
        argv = plan_apply.build_plan_argv(
            _cfg(
                TP_TARGET_ADDRS='["aws_s3_bucket.a"]',
                TP_REPLACE_ADDRS='["aws_instance.b"]',
            ),
            binary="tofu",
            var_file_args=["-var-file=x.tfvars"],
        )
        assert "-var-file=x.tfvars" in argv
        assert "-target=aws_s3_bucket.a" in argv
        assert "-replace=aws_instance.b" in argv


class TestBuildApplyArgv:
    def test_with_plan_file_minimal(self) -> None:
        argv = plan_apply.build_apply_argv(
            _cfg(), binary="tofu", var_file_args=["-var-file=x.tfvars"], has_plan_file=True
        )
        # With a plan file, var-files MUST NOT be re-specified.
        assert argv == ["tofu", "apply", "-input=false", "tfplan"]

    def test_without_plan_file_uses_auto_approve(self) -> None:
        argv = plan_apply.build_apply_argv(
            _cfg(TP_TARGET_ADDRS='["mod.a"]', TP_ALLOW_EMPTY_APPLY="true"),
            binary="tofu",
            var_file_args=["-var-file=foo.tfvars"],
            has_plan_file=False,
        )
        assert "-auto-approve" in argv
        assert "-var-file=foo.tfvars" in argv
        assert "-target=mod.a" in argv
        assert "-allow-empty-apply" in argv
        assert "tfplan" not in argv


class TestRunPlan:
    def test_exit_2_normalised_to_has_changes(self) -> None:
        with patch("terrapod.runner.exec_subprocess.run", return_value=_Result(2)):
            r = plan_apply.run_plan(
                _cfg(), binary="tofu", var_file_args=[], log_file="/tmp/plan.log"
            )
        assert r.exit_code == 0
        assert r.has_changes is True

    def test_exit_0_no_changes(self) -> None:
        with patch("terrapod.runner.exec_subprocess.run", return_value=_Result(0)):
            r = plan_apply.run_plan(
                _cfg(), binary="tofu", var_file_args=[], log_file="/tmp/plan.log"
            )
        assert r.exit_code == 0
        assert r.has_changes is False

    def test_exit_1_errored(self) -> None:
        with patch("terrapod.runner.exec_subprocess.run", return_value=_Result(1)):
            r = plan_apply.run_plan(
                _cfg(), binary="tofu", var_file_args=[], log_file="/tmp/plan.log"
            )
        assert r.exit_code == 1
        assert r.has_changes is False


class TestRunApply:
    def test_returns_exit_code(self) -> None:
        with patch("terrapod.runner.exec_subprocess.run", return_value=_Result(0)):
            rc = plan_apply.run_apply(
                _cfg(),
                binary="tofu",
                var_file_args=[],
                log_file="/tmp/apply.log",
                has_plan_file=True,
            )
        assert rc == 0


class TestRunPlanShowJson:
    def test_success_writes_file(self, tmp_path) -> None:
        out = tmp_path / "plan.json"

        def fake_run(cmd, check, stdout, stderr, timeout):
            stdout.write(b'{"resource_changes": []}')

            class R:
                returncode = 0
                stderr = b""

            return R()

        with patch("subprocess.run", side_effect=fake_run):
            ok = plan_apply.run_plan_show_json(binary="tofu", json_out=str(out))
        assert ok is True
        assert out.read_bytes().startswith(b"{")

    def test_nonzero_returns_false(self, tmp_path) -> None:
        out = tmp_path / "plan.json"

        def fake_run(cmd, check, stdout, stderr, timeout):
            class R:
                returncode = 1
                stderr = b"bad"

            return R()

        with patch("subprocess.run", side_effect=fake_run):
            ok = plan_apply.run_plan_show_json(binary="tofu", json_out=str(out))
        assert ok is False
        assert not out.exists()

    def test_timeout_returns_false(self, tmp_path) -> None:
        out = tmp_path / "plan.json"
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=120),
        ):
            ok = plan_apply.run_plan_show_json(binary="tofu", json_out=str(out))
        assert ok is False
