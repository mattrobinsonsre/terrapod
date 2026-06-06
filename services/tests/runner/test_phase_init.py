"""Tests for terrapod.runner.phases.init_phase."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from terrapod.runner.phases import init_phase


class _Result:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class TestRunInit:
    def test_success_returns_none(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(argv, log_file, child_grace_seconds, tee_to_stdout):
            captured["argv"] = argv
            return _Result(0)

        with (
            patch("terrapod.runner.exec_subprocess.run", side_effect=fake_run),
            patch(
                "terrapod.runner.phases.init_phase.init_supports_var_file",
                return_value=True,
            ),
        ):
            init_phase.run_init(
                binary="tofu",
                var_file_args=["-var-file=foo.tfvars"],
                log_file="/tmp/init.log",
            )
        assert captured["argv"] == ["tofu", "init", "-input=false", "-var-file=foo.tfvars"]

    def test_nonzero_raises(self) -> None:
        with (
            patch("terrapod.runner.exec_subprocess.run", return_value=_Result(1)),
            patch(
                "terrapod.runner.phases.init_phase.init_supports_var_file",
                return_value=True,
            ),
        ):
            with pytest.raises(init_phase.InitError) as ei:
                init_phase.run_init(binary="tofu", var_file_args=[], log_file="/tmp/x")
            assert ei.value.exit_code == 1

    def test_drops_var_file_when_binary_does_not_support(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(argv, log_file, child_grace_seconds, tee_to_stdout):
            captured["argv"] = argv
            return _Result(0)

        with (
            patch("terrapod.runner.exec_subprocess.run", side_effect=fake_run),
            patch(
                "terrapod.runner.phases.init_phase.init_supports_var_file",
                return_value=False,
            ),
        ):
            init_phase.run_init(
                binary="terraform",
                var_file_args=["-var-file=foo.tfvars"],
                log_file="/tmp/init.log",
            )
        assert captured["argv"] == ["terraform", "init", "-input=false"]

    def test_no_var_files_no_probe(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(argv, log_file, child_grace_seconds, tee_to_stdout):
            captured["argv"] = argv
            return _Result(0)

        with (
            patch("terrapod.runner.exec_subprocess.run", side_effect=fake_run),
            patch("terrapod.runner.phases.init_phase.init_supports_var_file") as probe,
        ):
            init_phase.run_init(binary="tofu", var_file_args=[], log_file="/tmp/x")
            probe.assert_not_called()
        assert captured["argv"] == ["tofu", "init", "-input=false"]
