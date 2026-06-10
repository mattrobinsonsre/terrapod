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


class TestStateDigest:
    def test_returns_none_for_missing_file(self, tmp_path) -> None:
        assert job_entrypoint._state_digest(tmp_path / "absent.tfstate") is None

    def test_returns_none_for_empty_file(self, tmp_path) -> None:
        p = tmp_path / "empty.tfstate"
        p.write_bytes(b"")
        assert job_entrypoint._state_digest(p) is None

    def test_hashes_content(self, tmp_path) -> None:
        p = tmp_path / "s.tfstate"
        p.write_bytes(b'{"serial": 8}')
        d1 = job_entrypoint._state_digest(p)
        assert d1 is not None and len(d1) == 64
        # Identical content → identical digest; changed content → different.
        p.write_bytes(b'{"serial": 8}')
        assert job_entrypoint._state_digest(p) == d1
        p.write_bytes(b'{"serial": 9}')
        assert job_entrypoint._state_digest(p) != d1


class TestApplyPhaseStateUploadSkip:
    """Serial-neutral no-op apply: when tofu applies but leaves the state
    byte-identical (a perpetual phantom diff, e.g. auth0 write-only secrets),
    the runner must NOT upload — there is nothing to persist and a re-upload at
    the unchanged serial would be mis-flagged as a state divergence."""

    def _cfg(self):
        from terrapod.runner.runner_config import RunnerConfig

        # has_api False (empty TP_API_URL) skips the plan-file / plan-artifacts
        # downloads so the test exercises only the apply + state-upload decision.
        return RunnerConfig.from_env(
            env={"TP_API_URL": "", "TP_RUN_ID": "", "TP_BACKEND": "tofu", "TP_VERSION": "1.12.1"}
        )

    def _run(self, tmp_path, *, apply_writes: bytes | None):
        state = tmp_path / "terraform.tfstate"
        state.write_bytes(b'{"serial": 164, "stable": true}')

        def _fake_run_apply(cfg, **kwargs):
            if apply_writes is not None:
                state.write_bytes(apply_writes)
            return 0

        with (
            patch.object(job_entrypoint.plan_apply, "run_apply", side_effect=_fake_run_apply),
            patch.object(job_entrypoint.uploads, "upload_state", return_value=True) as up,
            patch.object(job_entrypoint.uploads, "signal_state_diverged") as div,
            patch.object(job_entrypoint.uploads, "post_apply_result") as par,
        ):
            rc = job_entrypoint._run_apply_phase(
                self._cfg(),
                binary="/tmp/bin/tofu",
                var_file_argv=[],
                strip_dir=tmp_path,
                child_grace=10.0,
            )
        return rc, up, div, par

    def test_skips_upload_when_state_unchanged(self, tmp_path) -> None:
        # apply_writes=None → run_apply leaves the state file untouched.
        rc, up, div, par = self._run(tmp_path, apply_writes=None)
        assert rc == 0
        up.assert_not_called()
        div.assert_not_called()
        par.assert_called_once()

    def test_skips_upload_when_apply_rewrites_identical_bytes(self, tmp_path) -> None:
        # tofu rewrites the file but with byte-identical content (serial unchanged).
        rc, up, div, par = self._run(tmp_path, apply_writes=b'{"serial": 164, "stable": true}')
        assert rc == 0
        up.assert_not_called()

    def test_uploads_when_state_changed(self, tmp_path) -> None:
        rc, up, div, par = self._run(tmp_path, apply_writes=b'{"serial": 165, "changed": true}')
        assert rc == 0
        up.assert_called_once()
        div.assert_not_called()


class TestModuleSurface:
    def test_main_callable(self) -> None:
        assert callable(job_entrypoint.main)

    def test_logging_idempotent(self) -> None:
        job_entrypoint._configure_logging()
        job_entrypoint._configure_logging()

    def test_no_bash_entrypoint_path_constant(self) -> None:
        # The bash-handoff escape hatch is gone in v0.32.1.
        assert not hasattr(job_entrypoint, "_BASH_ENTRYPOINT_PATH")
