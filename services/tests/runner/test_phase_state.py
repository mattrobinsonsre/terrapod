"""Tests for terrapod.runner.phases.state."""

from __future__ import annotations

import httpx

from terrapod.runner.phases import state as state_phase
from terrapod.runner.runner_config import RunnerConfig


def _cfg(phase: str = "plan", **overrides) -> RunnerConfig:
    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
        "TP_DOWNLOAD_RETRY_DELAY": "0",
        "TP_PHASE": phase,
    }
    base.update(overrides)
    return RunnerConfig.from_env(env=base)


class TestDownloadState:
    def test_downloads_state_to_strip_dir(self, tmp_path) -> None:
        body = b'{"version":4,"terraform_version":"1.12.1","outputs":{}}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        present = state_phase.download_state(_cfg(), strip_dir=tmp_path, client=client)
        assert present
        assert (tmp_path / "terraform.tfstate").read_bytes() == body

    def test_404_means_no_state_yet(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        present = state_phase.download_state(_cfg(), strip_dir=tmp_path, client=client)
        assert present is False
        assert not (tmp_path / "terraform.tfstate").exists()

    def test_no_api_context_returns_false(self, tmp_path) -> None:
        present = state_phase.download_state(_cfg(TP_API_URL="", TP_RUN_ID=""), strip_dir=tmp_path)
        assert present is False


class TestReusePlanLockFile:
    def test_apply_phase_reuses_lock_file(self, tmp_path) -> None:
        body = b'provider "registry.opentofu.org/hashicorp/random" {\n  version = "3.6.0"\n}\n'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reused = state_phase.reuse_plan_lock_file(
            _cfg(phase="apply"), strip_dir=tmp_path, client=client
        )
        assert reused
        assert (tmp_path / ".terraform.lock.hcl").read_bytes() == body

    def test_plan_phase_does_not_call_lock_endpoint(self, tmp_path) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=b"x")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reused = state_phase.reuse_plan_lock_file(
            _cfg(phase="plan"), strip_dir=tmp_path, client=client
        )
        assert reused is False
        assert calls["n"] == 0
        assert not (tmp_path / ".terraform.lock.hcl").exists()

    def test_404_cleans_up_partial_file(self, tmp_path) -> None:
        # Seed a stale lock file to ensure it gets removed on miss.
        (tmp_path / ".terraform.lock.hcl").write_text("stale\n")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        reused = state_phase.reuse_plan_lock_file(
            _cfg(phase="apply"), strip_dir=tmp_path, client=client
        )
        assert reused is False
        assert not (tmp_path / ".terraform.lock.hcl").exists()
