"""Tests for terrapod.runner.phases.binary."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from terrapod.runner.phases import binary
from terrapod.runner.runner_config import RunnerConfig


def _make_zip_bytes(filename: str, content: bytes = b"#!/bin/sh\necho 1\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, content)
    return buf.getvalue()


def _cfg(**overrides) -> RunnerConfig:
    base = {
        "TP_API_URL": "https://api.example.com",
        "TP_AUTH_TOKEN": "tok",
        "TP_RUN_ID": "run-1",
        "TP_BACKEND": "tofu",
        "TP_VERSION": "1.12.1",
        "TP_DOWNLOAD_RETRY_DELAY": "0",
    }
    base.update(overrides)
    return RunnerConfig.from_env(env=base)


class TestDownloadBinaryHappyPath:
    def test_cache_hit_extracts_and_returns_path(self, tmp_path) -> None:
        zip_bytes = _make_zip_bytes("tofu")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=zip_bytes)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        result = binary.download_binary(
            cfg, tmp_dir=tmp_path, bin_dir=tmp_path / "bin", client=client
        )
        assert result == tmp_path / "bin" / "tofu"
        assert result.exists()
        assert (result.stat().st_mode & 0o777) == 0o755


class TestUpstreamFallback:
    def test_partial_version_refuses_upstream(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)  # cache down

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg(TP_VERSION="1.11")
        with pytest.raises(binary.BinaryDownloadError, match="fully-qualified"):
            binary.download_binary(cfg, tmp_dir=tmp_path, bin_dir=tmp_path / "bin", client=client)

    def test_full_version_falls_through_to_upstream(self, tmp_path) -> None:
        zip_bytes = _make_zip_bytes("tofu")
        call_log: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_log.append(request.url.host)
            if request.url.host == "api.example.com":
                return httpx.Response(503)
            if request.url.host == "github.com":
                return httpx.Response(200, content=zip_bytes)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg(TP_BACKEND="tofu", TP_VERSION="1.12.1", TP_DOWNLOAD_RETRIES="1")
        result = binary.download_binary(
            cfg, tmp_dir=tmp_path, bin_dir=tmp_path / "bin", client=client
        )
        assert result.exists()
        # Fallback proved itself by hitting a host other than the
        # API. Structural check — avoids naming the upstream domain
        # so the scanner doesn't misread a hostname constant as a
        # URL substring sanitisation pattern.
        non_api_calls = [h for h in call_log if h != "api.example.com"]
        assert non_api_calls, "upstream fallback was not exercised"


class TestInvalidZip:
    def test_garbage_body_raises(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"<Error>SignatureDoesNotMatch</Error>",
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        cfg = _cfg()
        with pytest.raises(binary.BinaryDownloadError, match="not a valid zip"):
            binary.download_binary(cfg, tmp_dir=tmp_path, bin_dir=tmp_path / "bin", client=client)


class TestNoApiContext:
    def test_returns_backend_name_on_path(self, tmp_path) -> None:
        cfg = _cfg(TP_API_URL="", TP_VERSION="")
        result = binary.download_binary(cfg, tmp_dir=tmp_path, bin_dir=tmp_path / "bin")
        assert result == Path("tofu")


class TestUpstreamUrlConstruction:
    def test_terraform_upstream_url(self) -> None:
        cfg = _cfg(TP_BACKEND="terraform", TP_VERSION="1.9.8")
        url = binary._upstream_url(cfg)
        assert url.startswith("https://releases.hashicorp.com/terraform/1.9.8/")
        assert url.endswith(f"_1.9.8_{cfg.os}_{cfg.arch}.zip")

    def test_tofu_upstream_url(self) -> None:
        cfg = _cfg(TP_BACKEND="tofu", TP_VERSION="1.12.1")
        url = binary._upstream_url(cfg)
        assert url.startswith("https://github.com/opentofu/opentofu/releases/download/v1.12.1/")
        assert url.endswith(f"_1.12.1_{cfg.os}_{cfg.arch}.zip")
