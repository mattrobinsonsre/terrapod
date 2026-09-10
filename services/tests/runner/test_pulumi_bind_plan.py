"""Saving and applying the preview's plan is an opt-in for Pulumi (#1553).

By default a Pulumi run previews and then runs a plain `pulumi up`: the preview
saves nothing, nothing crosses the pod boundary, and the update works out its own
changes — how Pulumi is normally run. A workspace that opts in gets the
`preview --save-plan` / `up --plan` binding, which rests on Pulumi's update plans
and is still experimental upstream.

Pinned at the three places the choice is made: the engine decides whether to tell
the Job, the argv builders decide what the CLI is asked to do, and the phase
runner decides whether a plan file is uploaded or fetched.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from terrapod.engines import strategy_for
from terrapod.runner.phases import pulumi_exec


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.target_urns = None
    return cfg


class TestTheEngineOnlyAsksWhenTheWorkspaceOptsIn:
    def _env(self, attrs: dict) -> dict[str, str]:
        s = strategy_for("pulumi")
        opts = s.options_from_attrs({"pulumi-stack": "default/p/d", **attrs}, "plan")
        return {e["name"]: e["value"] for e in s.container_env(opts, MagicMock())}

    def test_unbound_by_default(self) -> None:
        assert "TP_PULUMI_BIND_PLAN" not in self._env({})

    def test_explicitly_off_is_unbound(self) -> None:
        assert "TP_PULUMI_BIND_PLAN" not in self._env({"pulumi-bind-plan": False})

    def test_on_tells_the_job(self) -> None:
        assert self._env({"pulumi-bind-plan": True})["TP_PULUMI_BIND_PLAN"] == "true"


class TestTheRunnerReadsIt:
    def test_absent_is_unbound(self, monkeypatch) -> None:
        """Also what a listener older than the setting sends."""
        monkeypatch.delenv("TP_PULUMI_BIND_PLAN", raising=False)
        assert pulumi_exec.bind_plan_enabled() is False

    @pytest.mark.parametrize(
        ("value", "want"), [("true", True), ("TRUE", True), ("false", False), ("", False)]
    )
    def test_only_true_binds(self, monkeypatch, value, want) -> None:
        monkeypatch.setenv("TP_PULUMI_BIND_PLAN", value)
        assert pulumi_exec.bind_plan_enabled() is want


class TestTheArgv:
    def test_an_unbound_preview_saves_nothing(self) -> None:
        argv = pulumi_exec.preview_argv("", _cfg())
        assert argv[0] == "preview"
        assert not any(a.startswith("--save-plan") for a in argv)

    def test_a_bound_preview_saves_its_plan(self) -> None:
        assert "--save-plan=/workspace/plan.json" in pulumi_exec.preview_argv(
            "/workspace/plan.json", _cfg()
        )

    def test_an_unbound_update_is_a_plain_up(self) -> None:
        argv = pulumi_exec.update_argv("", _cfg())
        assert argv[:2] == ["up", "--yes"]
        assert not any(a.startswith("--plan") for a in argv)


class TestThePhaseRunner:
    """No plan file is uploaded after an unbound preview, and none is fetched
    before an unbound update — the hand-off exists only for the opt-in."""

    def _run(self, monkeypatch, *, phase: str, bind: bool) -> dict:
        from terrapod.runner import exec_subprocess, job_entrypoint
        from terrapod.runner.phases import platform_tool, uploads

        seen: dict = {"uploads": 0, "fetches": 0}
        monkeypatch.setenv("TP_PULUMI_PHASE", phase)
        if bind:
            monkeypatch.setenv("TP_PULUMI_BIND_PLAN", "true")
        else:
            monkeypatch.delenv("TP_PULUMI_BIND_PLAN", raising=False)
        monkeypatch.setattr(platform_tool, "ensure_tool", lambda cfg, tool: Path("/bin/pulumi"))

        def fake_run(argv, **_):
            seen["argv"] = argv
            if phase == "preview" and bind:
                # The real CLI writes the plan; the upload path needs it to exist.
                Path(argv[2].split("=", 1)[1]).write_text("{}")
            return MagicMock(exit_code=0)

        monkeypatch.setattr(exec_subprocess, "run", fake_run)
        monkeypatch.setattr(
            uploads,
            "upload_plan_file",
            lambda cfg, p: seen.__setitem__("uploads", seen["uploads"] + 1),
        )

        def fake_fetch(cfg, plan_file):
            seen["fetches"] += 1
            return True

        monkeypatch.setattr(job_entrypoint, "_fetch_pulumi_plan", fake_fetch)
        # The stack set-up and hand-back have tests of their own
        # (test_pulumi_local_state.py); here they only need to get out of the way.
        monkeypatch.setattr(
            pulumi_exec,
            "prepare_local_stack",
            lambda *a, **k: MagicMock(keys=pulumi_exec.StackKeys("v1:salt", "pw")),
        )
        monkeypatch.setattr(job_entrypoint, "_hand_back_pulumi_state", lambda *a, **k: 0)
        cfg = _cfg()
        cfg.phase = phase
        cfg.plan_only = False
        cfg.has_api = True
        cfg.api_url = "http://api"
        cfg.auth_token = "t"
        assert job_entrypoint._run_pulumi_phase(cfg, child_grace=5) == 0
        return seen

    def test_unbound_preview_uploads_nothing(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TP_PULUMI_PLAN_FILE", str(tmp_path / "plan.json"))
        seen = self._run(monkeypatch, phase="preview", bind=False)
        assert not any("--save-plan" in a for a in seen["argv"])
        assert seen["uploads"] == 0

    def test_unbound_update_fetches_nothing_and_runs_plain_up(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TP_PULUMI_PLAN_FILE", str(tmp_path / "plan.json"))
        seen = self._run(monkeypatch, phase="update", bind=False)
        assert seen["fetches"] == 0
        assert not any(a.startswith("--plan") for a in seen["argv"])

    def test_bound_preview_saves_and_uploads(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TP_PULUMI_PLAN_FILE", str(tmp_path / "plan.json"))
        seen = self._run(monkeypatch, phase="preview", bind=True)
        assert any(a.startswith("--save-plan=") for a in seen["argv"])
        assert seen["uploads"] == 1

    def test_bound_update_fetches_and_applies_the_plan(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("TP_PULUMI_PLAN_FILE", str(tmp_path / "plan.json"))
        seen = self._run(monkeypatch, phase="update", bind=True)
        assert seen["fetches"] == 1
        assert any(a.startswith("--plan=") for a in seen["argv"])
