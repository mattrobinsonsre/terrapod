"""Tests for terrapod.runner.phases.configuration."""

from __future__ import annotations

import io
import tarfile

import httpx

from terrapod.runner.phases import configuration as cfg_phase
from terrapod.runner.runner_config import RunnerConfig


def _make_tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
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


class TestDownloadConfiguration:
    def test_extracts_into_work_dir_and_writes_override(self, tmp_path) -> None:
        tar_bytes = _make_tarball(
            {
                "main.tf": 'terraform { cloud { organization = "default" } }\n',
                "variables.tf": 'variable "x" { type = string }\n',
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=tar_bytes)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        work_dir = tmp_path / "workspace"
        result = cfg_phase.download_configuration(_cfg(), work_dir=work_dir, client=client)

        assert result.downloaded
        assert result.strip_dir == work_dir
        assert (work_dir / "main.tf").exists()
        assert result.override_file is not None
        override = result.override_file.read_text()
        assert 'backend "local"' in override

    def test_working_directory_selects_subpath(self, tmp_path) -> None:
        tar_bytes = _make_tarball(
            {
                "envs/dev/main.tf": "# nested\n",
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=tar_bytes)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        work_dir = tmp_path / "workspace"
        result = cfg_phase.download_configuration(
            _cfg(TP_WORKING_DIR="envs/dev"), work_dir=work_dir, client=client
        )

        assert result.downloaded
        assert result.strip_dir == work_dir / "envs" / "dev"
        # The override file goes in strip_dir, not work_dir root.
        assert result.override_file == result.strip_dir / "zzzz_terrapod_backend_override.tf"
        assert result.override_file.exists()

    def test_missing_api_context_returns_undownloaded(self, tmp_path) -> None:
        result = cfg_phase.download_configuration(
            _cfg(TP_API_URL="", TP_RUN_ID=""),
            work_dir=tmp_path / "workspace",
        )
        assert result.downloaded is False
        assert result.override_file is None

    def test_download_404_returns_undownloaded(self, tmp_path) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        result = cfg_phase.download_configuration(
            _cfg(), work_dir=tmp_path / "workspace", client=client
        )
        assert result.downloaded is False


class TestSafeExtract:
    def test_path_traversal_member_is_skipped(self, tmp_path) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="../escape.txt")
            payload = b"pwned"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        tar_bytes = buf.getvalue()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=tar_bytes)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        work_dir = tmp_path / "workspace"
        cfg_phase.download_configuration(_cfg(), work_dir=work_dir, client=client)

        # Should not have escaped to tmp_path/escape.txt.
        assert not (tmp_path / "escape.txt").exists()
