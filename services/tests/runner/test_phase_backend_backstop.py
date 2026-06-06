"""Tests for terrapod.runner.phases.backend_backstop."""

from __future__ import annotations

import json

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
