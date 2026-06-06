"""Tests for terrapod.runner.phases.log_capture."""

from __future__ import annotations

import sys

import httpx

from terrapod.runner.phases import log_capture
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


class TestLogCapture:
    def test_tees_stdout_writes(self, tmp_path, capsys) -> None:
        out = tmp_path / "combined.log"
        with log_capture.LogCapture(out):
            print("hello-stdout", flush=True)
        # Restored — original stdout/stderr still wire to capsys.
        assert b"hello-stdout" in out.read_bytes()

    def test_tees_stderr_writes(self, tmp_path) -> None:
        out = tmp_path / "combined.log"
        with log_capture.LogCapture(out):
            sys.stderr.write("err-line\n")
            sys.stderr.flush()
        assert b"err-line" in out.read_bytes()

    def test_truncates_existing_log_on_enter(self, tmp_path) -> None:
        out = tmp_path / "combined.log"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("stale data from a previous run\n")
        with log_capture.LogCapture(out):
            sys.stdout.write("fresh\n")
            sys.stdout.flush()
        text = out.read_text()
        assert "stale data" not in text
        assert "fresh" in text

    def test_append_file_concatenates(self, tmp_path) -> None:
        out = tmp_path / "combined.log"
        phase_log = tmp_path / "plan.log"
        phase_log.write_text("plan-phase-output\n")
        with log_capture.LogCapture(out) as cap:
            sys.stdout.write("before-append\n")
            sys.stdout.flush()
            cap.append_file(phase_log)
            sys.stdout.write("after-append\n")
            sys.stdout.flush()
        text = out.read_text()
        assert (
            text.index("before-append")
            < text.index("plan-phase-output")
            < text.index("after-append")
        )

    def test_append_file_missing_is_noop(self, tmp_path) -> None:
        out = tmp_path / "combined.log"
        with log_capture.LogCapture(out) as cap:
            cap.append_file(tmp_path / "nonexistent.log")
        # No crash; file exists and is just empty / no stderr.
        assert out.exists()

    def test_restores_original_writes_on_exit(self, tmp_path) -> None:
        out = tmp_path / "combined.log"
        orig_stdout_write = sys.stdout.write
        orig_stderr_write = sys.stderr.write
        with log_capture.LogCapture(out):
            assert sys.stdout.write is not orig_stdout_write
        assert sys.stdout.write is orig_stdout_write
        assert sys.stderr.write is orig_stderr_write


class TestUploadCombinedLog:
    def test_no_api_returns_false(self, tmp_path) -> None:
        f = tmp_path / "x.log"
        f.write_text("data")
        cfg = _cfg(TP_API_URL="", TP_RUN_ID="")
        assert log_capture.upload_combined_log(cfg, f, phase="plan") is False

    def test_missing_log_returns_false(self, tmp_path) -> None:
        cfg = _cfg()
        assert log_capture.upload_combined_log(cfg, tmp_path / "nope.log", phase="plan") is False

    def test_empty_log_returns_false(self, tmp_path) -> None:
        f = tmp_path / "empty.log"
        f.write_bytes(b"")
        cfg = _cfg()
        assert log_capture.upload_combined_log(cfg, f, phase="plan") is False

    def test_success_first_attempt(self, tmp_path) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content
            captured["ct"] = request.headers.get("Content-Type")
            return httpx.Response(204)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        f = tmp_path / "log"
        f.write_bytes(b"some log content")
        cfg = _cfg()
        assert log_capture.upload_combined_log(cfg, f, phase="plan", client=client) is True
        assert captured["url"].endswith("/api/terrapod/v1/runs/run-1/artifacts/plan-log")
        assert captured["body"] == b"some log content"
        assert captured["ct"] == "application/octet-stream"

    def test_retries_then_succeeds(self, tmp_path) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(204)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        f = tmp_path / "log"
        f.write_bytes(b"x")
        sleeps: list[float] = []
        cfg = _cfg()
        ok = log_capture.upload_combined_log(
            cfg, f, phase="apply", client=client, sleep=lambda s: sleeps.append(s)
        )
        assert ok is True
        assert attempts["n"] == 3
        assert sleeps == [2, 4]

    def test_all_attempts_fail_returns_false(self, tmp_path, capsys) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        f = tmp_path / "log"
        f.write_bytes(b"y")
        cfg = _cfg()
        ok = log_capture.upload_combined_log(
            cfg, f, phase="plan", client=client, sleep=lambda s: None
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "FATAL" in captured.err
        assert "plan-log" in captured.err
