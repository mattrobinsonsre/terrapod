"""Tests for terrapod.runner.phases.tf_args."""

from __future__ import annotations

from unittest.mock import patch

from terrapod.runner.phases import tf_args


class TestArgBuilders:
    def test_var_file_args(self) -> None:
        assert tf_args.var_file_args(["a.tfvars", "b.tfvars"]) == [
            "-var-file=a.tfvars",
            "-var-file=b.tfvars",
        ]

    def test_var_file_args_skips_empty(self) -> None:
        assert tf_args.var_file_args(["a.tfvars", "", "b.tfvars"]) == [
            "-var-file=a.tfvars",
            "-var-file=b.tfvars",
        ]

    def test_target_args(self) -> None:
        assert tf_args.target_args(["aws_s3_bucket.foo", "module.bar"]) == [
            "-target=aws_s3_bucket.foo",
            "-target=module.bar",
        ]

    def test_target_args_empty_list(self) -> None:
        assert tf_args.target_args([]) == []

    def test_replace_args(self) -> None:
        assert tf_args.replace_args(["aws_instance.foo"]) == ["-replace=aws_instance.foo"]


class TestInitSupportsVarFile:
    def test_returns_true_when_help_mentions_var_file(self) -> None:
        class FakeResult:
            stdout = "Usage: tofu init [options]\n  -var-file=...\n"
            stderr = ""

        with patch("subprocess.run", return_value=FakeResult()):
            assert tf_args.init_supports_var_file("tofu") is True

    def test_returns_false_when_help_omits_var_file(self) -> None:
        class FakeResult:
            stdout = "Usage: terraform init [options]\n  -backend=true\n"
            stderr = ""

        with patch("subprocess.run", return_value=FakeResult()):
            assert tf_args.init_supports_var_file("terraform") is False

    def test_returns_false_when_binary_missing(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            assert tf_args.init_supports_var_file("/nope") is False

    def test_returns_false_on_timeout(self) -> None:
        import subprocess

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10),
        ):
            assert tf_args.init_supports_var_file("tofu") is False
