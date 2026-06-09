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

    def test_working_directory_writes_override_in_subdir_but_strip_dir_is_root(
        self, tmp_path
    ) -> None:
        """When `working_dir` is set, the override file MUST land
        inside that subdirectory (so tofu/terraform picks it up via
        the override-file merge at init time), but the returned
        `strip_dir` MUST be the un-descended work_dir root.

        Regression for the v0.32.0 Python rewrite of the runner: the
        configuration phase was returning the descended path as
        `strip_dir`, and `_run_body` was then calling
        `working_dir.resolve_and_chdir(strip_dir, cfg.working_dir)`
        which descended again — producing
        `<work_dir>/<wd>/<wd>` (doesn't exist) and a confusing
        "working directory '…' not found in config" error for every
        workspace with a working_dir set (helios-auth0-local-dev run
        019eac5e was the report). The descent must happen exactly
        once and `resolve_and_chdir` is the single canonical site.
        """
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
        # CRITICAL invariant: returned strip_dir is the un-descended
        # work_dir, so `resolve_and_chdir` owns the single descent +
        # chdir + path-traversal guard.
        assert result.strip_dir == work_dir
        # But the override DOES land in the working subdir so tofu's
        # override-file merge picks it up.
        assert result.override_file is not None
        assert (
            result.override_file == work_dir / "envs" / "dev" / "zzzz_terrapod_backend_override.tf"
        )
        assert result.override_file.exists()
        assert 'backend "local"' in result.override_file.read_text()

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
