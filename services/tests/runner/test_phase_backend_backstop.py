"""Tests for terrapod.runner.phases.backend_backstop."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from terrapod.runner.phases import backend_backstop


def _write_state(strip_dir, *, backend_type):
    d = strip_dir / ".terraform"
    d.mkdir(parents=True, exist_ok=True)
    if backend_type is None:
        (d / "terraform.tfstate").write_text(json.dumps({}))
    else:
        (d / "terraform.tfstate").write_text(json.dumps({"backend": {"type": backend_type}}))


class TestVerifyLocalBackend:
    def test_local_backend_passes(self, tmp_path) -> None:
        _write_state(tmp_path, backend_type="local")
        assert backend_backstop.verify_local_backend(tmp_path) == "local"

    def test_remote_backend_raises(self, tmp_path) -> None:
        _write_state(tmp_path, backend_type="remote")
        with pytest.raises(backend_backstop.BackendBackstopError, match="remote"):
            backend_backstop.verify_local_backend(tmp_path)

    def test_s3_backend_raises(self, tmp_path) -> None:
        _write_state(tmp_path, backend_type="s3")
        with pytest.raises(backend_backstop.BackendBackstopError, match="s3"):
            backend_backstop.verify_local_backend(tmp_path)

    def test_missing_state_file_raises(self, tmp_path) -> None:
        with pytest.raises(backend_backstop.BackendBackstopError, match="MISSING"):
            backend_backstop.verify_local_backend(tmp_path)

    def test_malformed_state_file_raises(self, tmp_path) -> None:
        d = tmp_path / ".terraform"
        d.mkdir()
        (d / "terraform.tfstate").write_text("not json")
        with pytest.raises(backend_backstop.BackendBackstopError, match="MISSING"):
            backend_backstop.verify_local_backend(tmp_path)

    def test_state_without_backend_key_raises(self, tmp_path) -> None:
        _write_state(tmp_path, backend_type=None)
        with pytest.raises(backend_backstop.BackendBackstopError, match="MISSING"):
            backend_backstop.verify_local_backend(tmp_path)

    def test_local_backend_on_another_state_path_is_rejected(self, tmp_path) -> None:
        d = tmp_path / ".terraform"
        d.mkdir()
        (d / "terraform.tfstate").write_text(
            json.dumps({"backend": {"type": "local", "config": {"path": "alternate.tfstate"}}})
        )
        with pytest.raises(backend_backstop.BackendBackstopError, match="terraform.tfstate"):
            backend_backstop.verify_local_backend(tmp_path)

    def test_real_tofu_local_backend_cannot_divert_workspace_state(self, tmp_path) -> None:
        if not shutil.which("tofu"):
            pytest.skip("OpenTofu is not installed")
        (tmp_path / "main.tf").write_text(
            'terraform {\n  backend "local" {\n    path = "alternate.tfstate"\n  }\n}\n'
        )
        result = subprocess.run(
            ["tofu", "init", "-input=false", "-no-color"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        with pytest.raises(backend_backstop.BackendBackstopError, match="terraform.tfstate"):
            backend_backstop.verify_local_backend(tmp_path)

    def test_real_tofu_default_backend_uses_workspace_state_path(self, tmp_path) -> None:
        if not shutil.which("tofu"):
            pytest.skip("OpenTofu is not installed")
        (tmp_path / "main.tf").write_text('terraform {\n  backend "local" {}\n}\n')
        result = subprocess.run(
            ["tofu", "init", "-input=false", "-no-color"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert backend_backstop.verify_local_backend(tmp_path) == "local"
