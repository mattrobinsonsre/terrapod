"""Tests for terrapod.runner.job_entrypoint.

The Python entrypoint is a thin pre-flight that delegates to the
existing bash script via os.execvp — phases will migrate from bash
into this module in subsequent PRs. These tests pin the skeleton
contract so the porting work has a baseline.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from terrapod.runner import job_entrypoint


class TestMainDelegatesToBash:
    def test_execvp_called_with_bash_entrypoint(self, tmp_path) -> None:
        """When the bash entrypoint exists and is executable, the Python
        wrapper exec's into it with no munging of argv."""
        fake_bash = tmp_path / "entrypoint.sh"
        fake_bash.write_text("#!/bin/sh\nexit 0\n")
        fake_bash.chmod(0o755)

        with (
            patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(fake_bash)),
            patch.object(job_entrypoint.os, "execvp") as exec_mock,
        ):
            rc = job_entrypoint.main(argv=[])

        # execvp does not return on success; the mock just records.
        # We assert the call happened and the wrapper returned the
        # belt-and-braces 1 (since the mock didn't actually replace
        # the process).
        assert rc == 1
        exec_mock.assert_called_once_with(str(fake_bash), [str(fake_bash)])

    def test_extra_argv_passed_through(self, tmp_path) -> None:
        fake_bash = tmp_path / "entrypoint.sh"
        fake_bash.write_text("#!/bin/sh\nexit 0\n")
        fake_bash.chmod(0o755)

        with (
            patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(fake_bash)),
            patch.object(job_entrypoint.os, "execvp") as exec_mock,
        ):
            job_entrypoint.main(argv=["--foo", "bar"])

        exec_mock.assert_called_once_with(
            str(fake_bash),
            [str(fake_bash), "--foo", "bar"],
        )


class TestMainPreFlightChecks:
    def test_returns_127_when_bash_missing(self, tmp_path) -> None:
        missing = tmp_path / "absent.sh"
        with patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(missing)):
            rc = job_entrypoint.main(argv=[])
        assert rc == 127

    def test_returns_126_when_bash_not_executable(self, tmp_path) -> None:
        not_exec = tmp_path / "entrypoint.sh"
        not_exec.write_text("#!/bin/sh\nexit 0\n")
        not_exec.chmod(0o644)  # readable but not executable

        with (
            patch.object(job_entrypoint, "_BASH_ENTRYPOINT_PATH", str(not_exec)),
            # Defensive: even if access check is faulty, execvp must
            # not actually run during this test.
            patch.object(job_entrypoint.os, "execvp"),
        ):
            rc = job_entrypoint.main(argv=[])

        assert rc == 126


class TestModuleEntrypoint:
    def test_module_executable_via_python_m(self) -> None:
        """`python -m terrapod.runner.job_entrypoint` is the K8s Job
        spec's entrypoint. Sanity-check that the module loads and the
        `main` callable is exported."""
        assert callable(job_entrypoint.main)
        assert hasattr(job_entrypoint, "_BASH_ENTRYPOINT_PATH")
        # Constant matches the runner Dockerfile's COPY target.
        assert job_entrypoint._BASH_ENTRYPOINT_PATH == "/entrypoint.sh"

    def test_logging_configured_without_raising(self) -> None:
        """Logging setup must be idempotent — a second `main()` call in
        the same process (which won't happen in practice but we
        belt-and-brace) shouldn't blow up."""
        # Just exercise the path; structlog.configure is itself idempotent.
        job_entrypoint._configure_logging()
        job_entrypoint._configure_logging()


class TestModuleEndToEndExecvp:
    def test_real_execvp_into_short_script(self, tmp_path) -> None:
        """One real execvp call, without mocking, against a tiny shell
        script that exits 0. Runs the binary in a fork so the test
        process survives the exec. Catches argv plumbing bugs that a
        mock would silently paper over."""
        fake_bash = tmp_path / "entrypoint.sh"
        fake_bash.write_text("#!/bin/sh\nexit 0\n")
        fake_bash.chmod(0o755)

        pid = os.fork()
        if pid == 0:
            # Child: replace ourselves with the wrapper.
            with patch.object(
                job_entrypoint,
                "_BASH_ENTRYPOINT_PATH",
                str(fake_bash),
            ):
                try:
                    job_entrypoint.main(argv=[])
                except BaseException:  # noqa: BLE001 — child must die quickly
                    os._exit(99)
            os._exit(98)  # Unreachable if execvp worked

        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status), "child did not exit cleanly"
        assert os.WEXITSTATUS(status) == 0, (
            f"expected exit 0 from fake bash, got {os.WEXITSTATUS(status)}"
        )
