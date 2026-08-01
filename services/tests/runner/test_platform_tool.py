"""Runner-side platform-tool fetch (#1208).

The behaviours worth pinning are the ones that replaced a guarantee the image
used to give for free: the tool arrives, it is unpacked from whatever shape
upstream ships, and — when it does not arrive — the *right* thing happens for
the caller. The policy path must fail closed; the scan path must not take the
run down with it.
"""

from __future__ import annotations

import dataclasses
import io
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from terrapod.runner.phases import platform_tool
from terrapod.runner.phases.platform_tool import (
    UNPACK,
    PlatformToolError,
    ensure_tool,
    fetch_versions,
    tool_path_or_name,
)
from terrapod.runner.runner_config import RunnerConfig


def _cfg(**over) -> RunnerConfig:
    """A RunnerConfig with the fields these tests care about.

    `os`/`arch` are derived from the host in `from_env`, so tests that assert on
    them override the built config directly rather than through the env.
    """
    cfg = RunnerConfig.from_env(
        env={
            "TP_API_URL": "https://terrapod.test",
            "TP_AUTH_TOKEN": "tok",
            "TP_RUN_ID": "run-1",
            "TP_BACKEND": "tofu",
            "TP_VERSION": "1.12.1",
        }
    )
    fields = {"os": "linux", "arch": "amd64", **over}
    return dataclasses.replace(cfg, **fields)


def _ok_result():
    r = MagicMock()
    r.ok = True
    r.status = 200
    return r


def _bad_result(status: int = 404):
    r = MagicMock()
    r.ok = False
    r.status = status
    return r


class TestFetchVersions:
    def test_reads_the_pinned_versions(self):
        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "attributes": {
                        "opa-version": "1.19.0",
                        "trivy-version": "0.72.0",
                        "checkov-version": "3.3.8",
                    }
                }
            },
        )
        assert fetch_versions(_cfg(), client=client) == {
            "opa": "1.19.0",
            "trivy": "0.72.0",
            "checkov": "3.3.8",
        }

    def test_raises_on_a_non_200(self):
        """Without a version there is nothing to ask the cache for, and guessing
        one would fetch the wrong binary rather than fail."""
        client = MagicMock()
        client.get.return_value = MagicMock(status_code=503)
        with pytest.raises(PlatformToolError, match="could not read"):
            fetch_versions(_cfg(), client=client)

    def test_raises_when_the_api_reports_nothing(self):
        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200, json=lambda: {"data": {"attributes": {}}}
        )
        with pytest.raises(PlatformToolError, match="no platform-tool versions"):
            fetch_versions(_cfg(), client=client)


class TestEnsureTool:
    def test_unpacks_a_bare_binary(self, tmp_path: Path):
        """opa ships as a bare executable — nothing to extract."""

        def fake_download(url, dest, **kw):
            Path(dest).write_bytes(b"#!/opa\n")
            return _ok_result()

        with patch.object(platform_tool, "download_to_file", fake_download):
            got = ensure_tool(
                _cfg(),
                "opa",
                versions={"opa": "1.19.0"},
                tmp_dir=tmp_path,
                bin_dir=tmp_path / "bin",
            )
        assert got.read_bytes() == b"#!/opa\n"
        assert got.stat().st_mode & 0o111  # executable

    def test_extracts_the_member_from_a_tarball(self, tmp_path: Path):
        """trivy ships a .tar.gz whose only interesting member is the binary."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, payload in (("README.md", b"docs"), ("trivy", b"TRIVYBIN")):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
        blob = buf.getvalue()

        def fake_download(url, dest, **kw):
            Path(dest).write_bytes(blob)
            return _ok_result()

        with patch.object(platform_tool, "download_to_file", fake_download):
            got = ensure_tool(
                _cfg(),
                "trivy",
                versions={"trivy": "0.72.0"},
                tmp_dir=tmp_path,
                bin_dir=tmp_path / "bin",
            )
        assert got.read_bytes() == b"TRIVYBIN"

    def test_extracts_the_member_from_a_zip(self, tmp_path: Path):
        """checkov ships a PyInstaller bundle as dist/checkov inside a zip."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dist/checkov", b"CHECKOVBIN")
        blob = buf.getvalue()

        def fake_download(url, dest, **kw):
            Path(dest).write_bytes(blob)
            return _ok_result()

        with patch.object(platform_tool, "download_to_file", fake_download):
            got = ensure_tool(
                _cfg(),
                "checkov",
                versions={"checkov": "3.3.8"},
                tmp_dir=tmp_path,
                bin_dir=tmp_path / "bin",
            )
        assert got.read_bytes() == b"CHECKOVBIN"

    def test_a_second_call_reuses_the_first_fetch(self, tmp_path: Path):
        """The plan and apply phases of one run must not download twice."""
        calls = []

        def fake_download(url, dest, **kw):
            calls.append(url)
            Path(dest).write_bytes(b"x")
            return _ok_result()

        with patch.object(platform_tool, "download_to_file", fake_download):
            for _ in range(3):
                ensure_tool(
                    _cfg(),
                    "opa",
                    versions={"opa": "1.19.0"},
                    tmp_dir=tmp_path,
                    bin_dir=tmp_path / "bin",
                )
        assert len(calls) == 1

    def test_asks_the_cache_for_this_runner_architecture(self, tmp_path: Path):
        seen = []

        def fake_download(url, dest, **kw):
            seen.append(url)
            Path(dest).write_bytes(b"x")
            return _ok_result()

        with patch.object(platform_tool, "download_to_file", fake_download):
            ensure_tool(
                _cfg(arch="arm64"),
                "opa",
                versions={"opa": "1.19.0"},
                tmp_dir=tmp_path,
                bin_dir=tmp_path / "bin",
            )
        assert seen[0].endswith("/binary-cache/opa/1.19.0/linux/arm64")

    def test_a_download_failure_raises_rather_than_falling_back(self, tmp_path: Path):
        """There is no binary on PATH to fall back to any more, so returning a
        bare name would surface as a confusing 'not found' at exec time."""
        with patch.object(platform_tool, "download_to_file", lambda *a, **k: _bad_result(500)):
            with pytest.raises(PlatformToolError, match="could not download"):
                ensure_tool(
                    _cfg(),
                    "opa",
                    versions={"opa": "1.19.0"},
                    tmp_dir=tmp_path,
                    bin_dir=tmp_path / "bin",
                )

    def test_rejects_a_tool_it_does_not_know(self, tmp_path: Path):
        with pytest.raises(PlatformToolError, match="not a platform tool"):
            ensure_tool(_cfg(), "terraform", tmp_dir=tmp_path, bin_dir=tmp_path / "bin")

    def test_no_api_url_is_a_clear_error(self, tmp_path: Path):
        with pytest.raises(PlatformToolError, match="no Terrapod API URL"):
            ensure_tool(
                _cfg(api_url=""),
                "opa",
                versions={"opa": "1.19.0"},
                tmp_dir=tmp_path,
                bin_dir=tmp_path / "bin",
            )


class TestToolPathOrName:
    """The non-raising wrapper the scan phase uses."""

    def test_returns_a_reason_instead_of_raising(self):
        with patch.object(
            platform_tool, "ensure_tool", side_effect=PlatformToolError("upstream down")
        ):
            path, why = tool_path_or_name(_cfg(), "trivy")
        assert path == ""
        assert "upstream down" in why

    def test_returns_the_path_on_success(self, tmp_path: Path):
        with patch.object(platform_tool, "ensure_tool", return_value=tmp_path / "trivy"):
            path, why = tool_path_or_name(_cfg(), "trivy")
        assert path.endswith("trivy")
        assert why is None


def test_unpack_table_agrees_with_the_server_spec():
    """The runner declares the archive shapes itself because the server module
    imports terrapod.config, which the runner image does not carry. Two copies
    of the same fact drift silently, so assert they match — a server-side
    change to how a tool is packaged would otherwise leave the runner
    extracting from the wrong shape."""
    from terrapod.services.platform_tools import SPECS

    assert set(UNPACK) == set(SPECS)
    for tool, spec in SPECS.items():
        assert UNPACK[tool].kind == spec.archive, tool
        assert UNPACK[tool].member == spec.member, tool
