"""Tests for terrapod.runner.phases.execution_hooks (#619)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from terrapod.runner.phases import execution_hooks


def _write_hooks(monkeypatch, tmp_path: Path, hooks: list[dict]) -> Path:
    f = tmp_path / "execution-hooks.json"
    f.write_text(json.dumps(hooks), encoding="utf-8")
    monkeypatch.setattr(execution_hooks, "_HOOKS_FILE", f)
    return f


class TestRunPoint:
    def test_missing_file_noop(self, monkeypatch, tmp_path) -> None:
        # No hooks file → nothing runs, no error.
        monkeypatch.setattr(execution_hooks, "_HOOKS_FILE", tmp_path / "does-not-exist.json")
        execution_hooks.run_point("pre_init")

    def test_unreadable_file_noop(self, monkeypatch, tmp_path) -> None:
        f = tmp_path / "execution-hooks.json"
        f.write_text("not json{", encoding="utf-8")
        monkeypatch.setattr(execution_hooks, "_HOOKS_FILE", f)
        # Malformed hooks must never fail the run — absence-of-hooks semantics.
        execution_hooks.run_point("pre_init")

    def test_runs_only_matching_point(self, monkeypatch, tmp_path) -> None:
        marker = tmp_path / "ran"
        other = tmp_path / "other"
        _write_hooks(
            monkeypatch,
            tmp_path,
            [
                {"hook_point": "pre_plan", "name": "a", "script": f"touch {marker}"},
                {"hook_point": "post_apply", "name": "b", "script": f"touch {other}"},
            ],
        )
        execution_hooks.run_point("pre_plan")
        assert marker.exists()
        assert not other.exists()

    def test_runs_in_delivered_order(self, monkeypatch, tmp_path) -> None:
        out = tmp_path / "order"
        _write_hooks(
            monkeypatch,
            tmp_path,
            [
                {"hook_point": "pre_init", "name": "first", "script": f"echo 1 >> {out}"},
                {"hook_point": "pre_init", "name": "second", "script": f"echo 2 >> {out}"},
            ],
        )
        execution_hooks.run_point("pre_init")
        assert out.read_text().split() == ["1", "2"]

    def test_empty_script_skipped(self, monkeypatch, tmp_path) -> None:
        _write_hooks(
            monkeypatch,
            tmp_path,
            [{"hook_point": "pre_init", "name": "blank", "script": "   "}],
        )
        # A whitespace-only script must not error.
        execution_hooks.run_point("pre_init")

    def test_nonzero_raises_hookerror(self, monkeypatch, tmp_path) -> None:
        _write_hooks(
            monkeypatch,
            tmp_path,
            [{"hook_point": "post_apply", "name": "boom", "script": "exit 5"}],
        )
        with pytest.raises(execution_hooks.HookError) as ei:
            execution_hooks.run_point("post_apply")
        assert ei.value.exit_code == 5
        assert ei.value.name == "boom"
        assert ei.value.hook_point == "post_apply"

    def test_stops_at_first_failure(self, monkeypatch, tmp_path) -> None:
        later = tmp_path / "later"
        _write_hooks(
            monkeypatch,
            tmp_path,
            [
                {"hook_point": "pre_plan", "name": "fail", "script": "exit 1"},
                {"hook_point": "pre_plan", "name": "after", "script": f"touch {later}"},
            ],
        )
        with pytest.raises(execution_hooks.HookError):
            execution_hooks.run_point("pre_plan")
        assert not later.exists()

    def test_env_passthrough(self, monkeypatch, tmp_path) -> None:
        out = tmp_path / "env"
        _write_hooks(
            monkeypatch,
            tmp_path,
            [{"hook_point": "pre_init", "name": "e", "script": f"echo $TP_X > {out}"}],
        )
        execution_hooks.run_point("pre_init", env={"TP_X": "yes", "PATH": "/usr/bin:/bin"})
        assert out.read_text().strip() == "yes"
