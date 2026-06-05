"""Tests for terrapod.runner.job_entrypoint.

The Python entrypoint runs the ported phases, then hands off to the
bash script for unported phases. Tests pin the skeleton contract so
porting work has a baseline and so the env-var markers Bash relies on
are produced consistently.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from terrapod.runner import job_entrypoint


class TestMainDelegatesToBash:
    def test_execvpe_called_with_bash_entrypoint(self, tmp_path) -> None:
        """When the bash entrypoint exists and is executable, the Python
        wrapper exec's into it with no munging of argv."""
        fake_bash = tmp_path / "entrypoint.sh"
        fake_bash.write_text("#!/bin/sh\nexit 0\n")
        fake_bash.chmod(0o755)

        with (
            patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(fake_bash)),
            patch.object(job_entrypoint.os, "execvpe") as exec_mock,
        ):
            rc = job_entrypoint.main(argv=[])

        assert rc == 1  # belt-and-braces; mock doesn't actually replace process
        # Three-arg form: (path, argv-list, env-dict).
        assert exec_mock.call_count == 1
        args, _ = exec_mock.call_args
        assert args[0] == str(fake_bash)
        assert args[1] == [str(fake_bash)]
        assert isinstance(args[2], dict)

    def test_extra_argv_passed_through(self, tmp_path) -> None:
        fake_bash = tmp_path / "entrypoint.sh"
        fake_bash.write_text("#!/bin/sh\nexit 0\n")
        fake_bash.chmod(0o755)

        with (
            patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(fake_bash)),
            patch.object(job_entrypoint.os, "execvpe") as exec_mock,
        ):
            job_entrypoint.main(argv=["--foo", "bar"])

        args, _ = exec_mock.call_args
        assert args[1] == [str(fake_bash), "--foo", "bar"]

    def test_bash_env_includes_phase_markers(self, tmp_path) -> None:
        """The bash continuation must see TP_RUNNER_BINARY_DONE etc. so
        it skips the already-handled blocks. Without API context, the
        Python phases short-circuit but still emit the markers."""
        fake_bash = tmp_path / "entrypoint.sh"
        fake_bash.write_text("#!/bin/sh\nexit 0\n")
        fake_bash.chmod(0o755)

        with (
            patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(fake_bash)),
            patch.object(job_entrypoint.os, "execvpe") as exec_mock,
        ):
            job_entrypoint.main(argv=[])

        env = exec_mock.call_args[0][2]
        assert env.get("TP_RUNNER_BINARY_DONE") == "1"
        assert env.get("TP_RUNNER_CONFIGURATION_DONE") == "1"
        assert env.get("TP_RUNNER_STATE_DONE") == "1"
        # TP_BIN is set even on the no-API code path (bare backend name).
        assert env.get("TP_BIN") in ("tofu", "terraform")


class TestMainPreFlightChecks:
    def test_returns_127_when_bash_missing(self, tmp_path) -> None:
        missing = tmp_path / "absent.sh"
        with patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(missing)):
            rc = job_entrypoint.main(argv=[])
        assert rc == 127

    def test_returns_126_when_bash_not_executable(self, tmp_path) -> None:
        not_exec = tmp_path / "entrypoint.sh"
        not_exec.write_text("#!/bin/sh\nexit 0\n")
        not_exec.chmod(0o644)

        with (
            patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(not_exec)),
            patch.object(job_entrypoint.os, "execvpe"),
        ):
            rc = job_entrypoint.main(argv=[])

        assert rc == 126


class TestModuleEntrypoint:
    def test_module_executable_via_python_m(self) -> None:
        assert callable(job_entrypoint.main)
        assert hasattr(job_entrypoint, "_BASH_ENTRYPOINT_PATH")
        assert job_entrypoint._BASH_ENTRYPOINT_PATH == "/entrypoint.sh"

    def test_logging_idempotent(self) -> None:
        job_entrypoint._configure_logging()
        job_entrypoint._configure_logging()


class TestModuleEndToEndExecvp:
    def test_real_execvpe_into_short_script(self, tmp_path) -> None:
        """One real execvp call, without mocking, against a tiny shell
        script that exits 0. Forks so the test process survives the
        exec. Catches argv/env plumbing bugs that a mock would silently
        paper over."""
        fake_bash = tmp_path / "entrypoint.sh"
        fake_bash.write_text('#!/bin/sh\n[ "$TP_RUNNER_BINARY_DONE" = "1" ] && exit 0 || exit 42\n')
        fake_bash.chmod(0o755)

        pid = os.fork()
        if pid == 0:
            with patch.object(
                job_entrypoint,
                "_BASH_ENTRYPOINT_PATH",
                str(fake_bash),
            ):
                try:
                    job_entrypoint.main(argv=[])
                except Exception:
                    # In a forked child we must exit promptly with a
                    # distinct status so the parent can tell a real
                    # exec failure from a clean exit. Caught Exception
                    # (not BaseException) — SystemExit and KeyboardInterrupt
                    # are intentionally left to bubble.
                    os._exit(99)
            os._exit(98)

        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status), "child did not exit cleanly"
        # 0 means bash saw the marker we set; 42 means it didn't.
        assert os.WEXITSTATUS(status) == 0, (
            f"bash continuation didn't see TP_RUNNER_BINARY_DONE marker "
            f"(exit {os.WEXITSTATUS(status)})"
        )
