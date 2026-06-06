"""Tests for terrapod.runner.phases.setup_script."""

from __future__ import annotations

import pytest

from terrapod.runner.phases import setup_script


class TestRun:
    def test_empty_script_noop(self) -> None:
        # Returns cleanly even with no script.
        setup_script.run("")

    def test_zero_exit_returns_cleanly(self, tmp_path) -> None:
        sentinel = tmp_path / "ran"
        setup_script.run(f"touch {sentinel}")
        assert sentinel.exists()

    def test_nonzero_raises(self) -> None:
        with pytest.raises(setup_script.SetupScriptError) as ei:
            setup_script.run("exit 7")
        assert ei.value.exit_code == 7

    def test_env_passthrough(self, tmp_path) -> None:
        out = tmp_path / "out"
        setup_script.run(
            f"echo $TP_TEST_VAR > {out}",
            env={"TP_TEST_VAR": "hello", "PATH": "/usr/bin:/bin"},
        )
        assert out.read_text().strip() == "hello"
