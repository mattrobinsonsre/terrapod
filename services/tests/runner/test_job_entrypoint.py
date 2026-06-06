"""Tests for terrapod.runner.job_entrypoint.

The full orchestrator drives ten phases through subprocess + HTTP; an
end-to-end test would be a smoke test, not a unit test. These tests
pin the orchestrator-level invariants:

  - main() returns the body's exit code.
  - main() ALWAYS uploads the combined log and posts the resource
    profile, regardless of the body's outcome (the EXIT-trap
    equivalent).
  - BinaryDownloadError → exit code 1.
  - Unexpected exception → exit code 1 (but log is still uploaded).
  - WORK_DIR env var is honoured.
"""

from __future__ import annotations

from unittest.mock import patch

from terrapod.runner import job_entrypoint
from terrapod.runner.phases.binary import BinaryDownloadError


def _env(monkeypatch):
    monkeypatch.setenv("TP_API_URL", "https://api.example.com")
    monkeypatch.setenv("TP_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TP_RUN_ID", "run-1")
    monkeypatch.setenv("TP_BACKEND", "tofu")
    monkeypatch.setenv("TP_VERSION", "1.12.1")
    monkeypatch.setenv("TP_PHASE", "plan")


class TestMainReturnCode:
    def test_returns_body_exit_code(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=0) as body,
            patch.object(job_entrypoint.log_capture, "upload_combined_log"),
            patch.object(job_entrypoint.resource_profile, "post_profile"),
        ):
            rc = job_entrypoint.main()
        assert rc == 0
        body.assert_called_once()

    def test_propagates_non_zero(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=7),
            patch.object(job_entrypoint.log_capture, "upload_combined_log"),
            patch.object(job_entrypoint.resource_profile, "post_profile"),
        ):
            rc = job_entrypoint.main()
        assert rc == 7


class TestExitTrapEquivalent:
    def test_uploads_log_and_posts_profile_on_success(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=0),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            job_entrypoint.main()
        ulog.assert_called_once()
        upro.assert_called_once()
        # Profile body uses the body's exit code.
        assert upro.call_args.kwargs.get("exit_code") == 0 or upro.call_args.args[1] == 0

    def test_uploads_log_and_posts_profile_on_failure(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=42),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            rc = job_entrypoint.main()
        assert rc == 42
        ulog.assert_called_once()
        upro.assert_called_once()

    def test_uploads_log_even_when_body_raises(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", side_effect=RuntimeError("boom")),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            rc = job_entrypoint.main()
        assert rc == 1
        ulog.assert_called_once()
        upro.assert_called_once()


class TestBinaryDownloadError:
    def test_returns_1_and_still_uploads(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(
                job_entrypoint,
                "_run_body",
                side_effect=BinaryDownloadError("nope"),
            ),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            rc = job_entrypoint.main()
        assert rc == 1
        ulog.assert_called_once()
        upro.assert_called_once()


class TestWorkDirOverride:
    def test_honours_WORK_DIR_env(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        custom = tmp_path / "custom-work"
        monkeypatch.setenv("WORK_DIR", str(custom))
        seen: dict[str, object] = {}

        def fake_body(cfg, work_dir):
            seen["work_dir"] = work_dir
            return 0

        with (
            patch.object(job_entrypoint, "_run_body", side_effect=fake_body),
            patch.object(job_entrypoint.log_capture, "upload_combined_log"),
            patch.object(job_entrypoint.resource_profile, "post_profile"),
        ):
            job_entrypoint.main()
        assert str(seen["work_dir"]) == str(custom)


class TestModuleSurface:
    def test_main_callable(self) -> None:
        assert callable(job_entrypoint.main)

    def test_logging_idempotent(self) -> None:
        job_entrypoint._configure_logging()
        job_entrypoint._configure_logging()

    def test_no_bash_entrypoint_path_constant(self) -> None:
        # The bash-handoff escape hatch is gone in v0.32.1.
        assert not hasattr(job_entrypoint, "_BASH_ENTRYPOINT_PATH")
