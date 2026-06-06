"""Tests for terrapod.runner.phases.working_dir."""

from __future__ import annotations

import os

import pytest

from terrapod.runner.phases import working_dir


class TestResolveAndChdir:
    def test_no_subdir_chdirs_into_work_dir(self, tmp_path) -> None:
        result = working_dir.resolve_and_chdir(tmp_path, "")
        assert result == tmp_path
        assert os.getcwd() == str(tmp_path.resolve())

    def test_subdir_chdirs_into_subdir(self, tmp_path) -> None:
        sub = tmp_path / "infra"
        sub.mkdir()
        result = working_dir.resolve_and_chdir(tmp_path, "infra")
        assert result == sub.resolve()
        assert os.getcwd() == str(sub.resolve())

    def test_leading_slash_stripped(self, tmp_path) -> None:
        sub = tmp_path / "infra"
        sub.mkdir()
        result = working_dir.resolve_and_chdir(tmp_path, "/infra/")
        assert result == sub.resolve()

    def test_dotdot_rejected(self, tmp_path) -> None:
        with pytest.raises(working_dir.WorkingDirectoryError, match="path traversal"):
            working_dir.resolve_and_chdir(tmp_path, "../etc")

    def test_subdir_does_not_exist(self, tmp_path) -> None:
        with pytest.raises(working_dir.WorkingDirectoryError, match="not found"):
            working_dir.resolve_and_chdir(tmp_path, "missing")

    def test_subdir_is_a_file(self, tmp_path) -> None:
        f = tmp_path / "not-a-dir"
        f.write_text("hi")
        with pytest.raises(working_dir.WorkingDirectoryError, match="not found"):
            working_dir.resolve_and_chdir(tmp_path, "not-a-dir")
