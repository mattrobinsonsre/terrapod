"""Tests for terrapod.runner.phases.resource_profile."""

from __future__ import annotations

import httpx

from terrapod.runner.phases import resource_profile
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


class TestCollectProfile:
    def test_reads_both_files(self, tmp_path) -> None:
        mem = tmp_path / "memory.peak"
        cpu = tmp_path / "cpu.stat"
        mem.write_text("1234567\n")
        cpu.write_text("usage_usec 4242\nuser_usec 100\nsystem_usec 200\n")
        out = resource_profile.collect_profile(memory_peak_path=mem, cpu_stat_path=cpu, exit_code=0)
        assert out == {
            "exit_code": 0,
            "peak_memory_bytes": 1234567,
            "peak_cpu_usec": 4242,
        }

    def test_handles_missing_memory_file(self, tmp_path) -> None:
        cpu = tmp_path / "cpu.stat"
        cpu.write_text("usage_usec 4242\n")
        out = resource_profile.collect_profile(
            memory_peak_path=tmp_path / "no_mem",
            cpu_stat_path=cpu,
            exit_code=2,
        )
        assert out == {"exit_code": 2, "peak_cpu_usec": 4242}

    def test_handles_malformed_cpu_stat(self, tmp_path) -> None:
        mem = tmp_path / "memory.peak"
        cpu = tmp_path / "cpu.stat"
        mem.write_text("999\n")
        cpu.write_text("user_usec 1\nsystem_usec 2\n")  # no usage_usec
        out = resource_profile.collect_profile(memory_peak_path=mem, cpu_stat_path=cpu, exit_code=0)
        assert out == {"exit_code": 0, "peak_memory_bytes": 999}

    def test_both_files_missing(self, tmp_path) -> None:
        out = resource_profile.collect_profile(
            memory_peak_path=tmp_path / "nope",
            cpu_stat_path=tmp_path / "nope2",
            exit_code=137,
        )
        assert out == {"exit_code": 137}


class TestPostProfile:
    def test_no_api_returns_false(self, tmp_path) -> None:
        cfg = _cfg(TP_API_URL="", TP_RUN_ID="")
        assert resource_profile.post_profile(cfg, 0) is False

    def test_success_returns_true(self, tmp_path) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(204)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        mem = tmp_path / "memory.peak"
        mem.write_text("100\n")
        cpu = tmp_path / "cpu.stat"
        cpu.write_text("usage_usec 50\n")
        cfg = _cfg()
        ok = resource_profile.post_profile(
            cfg,
            exit_code=0,
            memory_peak_path=mem,
            cpu_stat_path=cpu,
            client=client,
        )
        assert ok is True
        assert captured["url"].endswith("/api/terrapod/v1/runs/run-1/resource-profile")
        assert captured["auth"] == "Bearer tok"
        assert captured["body"] == {
            "exit_code": 0,
            "peak_memory_bytes": 100,
            "peak_cpu_usec": 50,
        }

    def test_non_2xx_returns_false(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        assert resource_profile.post_profile(cfg, 1, client=client) is False

    def test_request_error_returns_false(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        # Never raises.
        assert resource_profile.post_profile(cfg, 1, client=client) is False
