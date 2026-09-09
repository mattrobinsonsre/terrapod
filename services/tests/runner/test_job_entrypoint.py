"""Tests for terrapod.runner.job_entrypoint.

The full orchestrator drives ten phases through subprocess + HTTP; an
end-to-end test would be a smoke test, not a unit test. These tests
pin the orchestrator-level invariants:

  - main() returns the body's exit code.
  - main() ALWAYS uploads the combined log and posts the resource
    profile, regardless of the body's outcome (the EXIT-trap
    equivalent).
  - BinaryDownloadError → exit code 1.
  - Unexpected exception → exit code 1 (but log is still uploaded).
  - WORK_DIR env var is honoured.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from terrapod.runner import job_entrypoint
from terrapod.runner.phases.binary import BinaryDownloadError


def _env(monkeypatch):
    monkeypatch.setenv("TP_API_URL", "https://api.example.com")
    monkeypatch.setenv("TP_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TP_RUN_ID", "run-1")
    monkeypatch.setenv("TP_BACKEND", "tofu")
    monkeypatch.setenv("TP_VERSION", "1.12.1")
    monkeypatch.setenv("TP_PHASE", "plan")


class TestMainReturnCode:
    def test_returns_body_exit_code(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=0) as body,
            patch.object(job_entrypoint.log_capture, "upload_combined_log"),
            patch.object(job_entrypoint.resource_profile, "post_profile"),
        ):
            rc = job_entrypoint.main()
        assert rc == 0
        body.assert_called_once()

    def test_propagates_non_zero(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=7),
            patch.object(job_entrypoint.log_capture, "upload_combined_log"),
            patch.object(job_entrypoint.resource_profile, "post_profile"),
        ):
            rc = job_entrypoint.main()
        assert rc == 7


class TestExitTrapEquivalent:
    def test_uploads_log_and_posts_profile_on_success(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=0),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            job_entrypoint.main()
        ulog.assert_called_once()
        upro.assert_called_once()
        # Profile body uses the body's exit code.
        assert upro.call_args.kwargs.get("exit_code") == 0 or upro.call_args.args[1] == 0

    def test_uploads_log_and_posts_profile_on_failure(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", return_value=42),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            rc = job_entrypoint.main()
        assert rc == 42
        ulog.assert_called_once()
        upro.assert_called_once()

    def test_uploads_log_even_when_body_raises(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(job_entrypoint, "_run_body", side_effect=RuntimeError("boom")),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            rc = job_entrypoint.main()
        assert rc == 1
        ulog.assert_called_once()
        upro.assert_called_once()


class TestBinaryDownloadError:
    def test_returns_1_and_still_uploads(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        monkeypatch.setenv("WORK_DIR", str(tmp_path))
        with (
            patch.object(
                job_entrypoint,
                "_run_body",
                side_effect=BinaryDownloadError("nope"),
            ),
            patch.object(job_entrypoint.log_capture, "upload_combined_log") as ulog,
            patch.object(job_entrypoint.resource_profile, "post_profile") as upro,
        ):
            rc = job_entrypoint.main()
        assert rc == 1
        ulog.assert_called_once()
        upro.assert_called_once()


class TestWorkDirOverride:
    def test_honours_WORK_DIR_env(self, monkeypatch, tmp_path) -> None:
        _env(monkeypatch)
        custom = tmp_path / "custom-work"
        monkeypatch.setenv("WORK_DIR", str(custom))
        seen: dict[str, object] = {}

        def fake_body(cfg, work_dir):
            seen["work_dir"] = work_dir
            return 0

        with (
            patch.object(job_entrypoint, "_run_body", side_effect=fake_body),
            patch.object(job_entrypoint.log_capture, "upload_combined_log"),
            patch.object(job_entrypoint.resource_profile, "post_profile"),
        ):
            job_entrypoint.main()
        assert str(seen["work_dir"]) == str(custom)


class TestStateDigest:
    def test_returns_none_for_missing_file(self, tmp_path) -> None:
        assert job_entrypoint._state_digest(tmp_path / "absent.tfstate") is None

    def test_returns_none_for_empty_file(self, tmp_path) -> None:
        p = tmp_path / "empty.tfstate"
        p.write_bytes(b"")
        assert job_entrypoint._state_digest(p) is None

    def test_hashes_content(self, tmp_path) -> None:
        p = tmp_path / "s.tfstate"
        p.write_bytes(b'{"serial": 8}')
        d1 = job_entrypoint._state_digest(p)
        assert d1 is not None and len(d1) == 64
        # Identical content → identical digest; changed content → different.
        p.write_bytes(b'{"serial": 8}')
        assert job_entrypoint._state_digest(p) == d1
        p.write_bytes(b'{"serial": 9}')
        assert job_entrypoint._state_digest(p) != d1


class TestApplyPhaseStateUploadSkip:
    """Serial-neutral no-op apply: when tofu applies but leaves the state
    byte-identical (a perpetual phantom diff, e.g. auth0 write-only secrets),
    the runner must NOT upload — there is nothing to persist and a re-upload at
    the unchanged serial would be mis-flagged as a state divergence."""

    def _cfg(self):
        from terrapod.runner.runner_config import RunnerConfig

        # has_api False (empty TP_API_URL) skips the plan-file / plan-artifacts
        # downloads so the test exercises only the apply + state-upload decision.
        return RunnerConfig.from_env(
            env={"TP_API_URL": "", "TP_RUN_ID": "", "TP_BACKEND": "tofu", "TP_VERSION": "1.12.1"}
        )

    def _run(self, tmp_path, *, apply_writes: bytes | None):
        state = tmp_path / "terraform.tfstate"
        state.write_bytes(b'{"serial": 164, "stable": true}')

        def _fake_run_apply(cfg, **kwargs):
            if apply_writes is not None:
                state.write_bytes(apply_writes)
            return 0

        with (
            patch.object(job_entrypoint.plan_apply, "run_apply", side_effect=_fake_run_apply),
            patch.object(job_entrypoint.uploads, "upload_state", return_value=True) as up,
            patch.object(job_entrypoint.uploads, "signal_state_diverged") as div,
            patch.object(job_entrypoint.uploads, "post_apply_result") as par,
        ):
            rc = job_entrypoint._run_apply_phase(
                self._cfg(),
                binary="/tmp/bin/tofu",
                var_file_argv=[],
                strip_dir=tmp_path,
                child_grace=10.0,
            )
        return rc, up, div, par

    def test_skips_upload_when_state_unchanged(self, tmp_path) -> None:
        # apply_writes=None → run_apply leaves the state file untouched.
        rc, up, div, par = self._run(tmp_path, apply_writes=None)
        assert rc == 0
        up.assert_not_called()
        div.assert_not_called()
        par.assert_called_once()

    def test_skips_upload_when_apply_rewrites_identical_bytes(self, tmp_path) -> None:
        # tofu rewrites the file but with byte-identical content (serial unchanged).
        rc, up, div, par = self._run(tmp_path, apply_writes=b'{"serial": 164, "stable": true}')
        assert rc == 0
        up.assert_not_called()

    def test_uploads_when_state_changed(self, tmp_path) -> None:
        rc, up, div, par = self._run(tmp_path, apply_writes=b'{"serial": 165, "changed": true}')
        assert rc == 0
        up.assert_called_once()
        div.assert_not_called()


class TestModuleSurface:
    def test_main_callable(self) -> None:
        assert callable(job_entrypoint.main)

    def test_logging_idempotent(self) -> None:
        job_entrypoint._configure_logging()
        job_entrypoint._configure_logging()

    def test_no_bash_entrypoint_path_constant(self) -> None:
        # The bash-handoff escape hatch is gone in v0.32.1.
        assert not hasattr(job_entrypoint, "_BASH_ENTRYPOINT_PATH")


class TestApplyPhaseExecutionHooks:
    """post_apply hook semantics (#619 / #671): a failing post_apply hook must
    error the run, but only AFTER state has been uploaded — the run surfaces as
    errored while the applied state is safely persisted."""

    def _cfg(self):
        from unittest.mock import MagicMock

        cfg = MagicMock()
        cfg.has_api = False  # skip plan-file download + plan-artifacts blocks
        return cfg

    def test_post_apply_failure_errors_run_after_state_upload(self, tmp_path) -> None:
        from unittest.mock import patch

        from terrapod.runner.phases import execution_hooks as eh

        strip = tmp_path
        points: list[str] = []

        def _apply(*_a, **_k):
            # apply writes state (so the upload branch runs, not the
            # serial-neutral skip).
            (strip / "terraform.tfstate").write_text('{"serial":1}')
            return 0

        def _run_point(point, env=None):
            points.append(point)
            if point == "post_apply":
                raise eh.HookError("post_apply", "boom", 5)

        with (
            patch.object(job_entrypoint.plan_apply, "run_apply", side_effect=_apply),
            patch.object(job_entrypoint.uploads, "upload_state", return_value=True) as up,
            patch.object(job_entrypoint.uploads, "post_apply_result") as par,
            patch.object(job_entrypoint.execution_hooks, "run_point", side_effect=_run_point),
        ):
            rc = job_entrypoint._run_apply_phase(
                self._cfg(), binary="tofu", var_file_argv=[], strip_dir=strip, child_grace=1.0
            )

        assert rc == 5, "run must error with the post_apply hook's exit code"
        up.assert_called_once()  # state WAS persisted before the failure
        par.assert_not_called()  # apply-result NOT posted (hook failed first)
        assert points == ["pre_apply", "post_apply"]

    def test_post_apply_success_posts_apply_result(self, tmp_path) -> None:
        from unittest.mock import patch

        strip = tmp_path
        points: list[str] = []

        def _apply(*_a, **_k):
            (strip / "terraform.tfstate").write_text('{"serial":1}')
            return 0

        def _run_point(point, env=None):
            points.append(point)

        with (
            patch.object(job_entrypoint.plan_apply, "run_apply", side_effect=_apply),
            patch.object(job_entrypoint.uploads, "upload_state", return_value=True) as up,
            patch.object(job_entrypoint.uploads, "post_apply_result") as par,
            patch.object(job_entrypoint.execution_hooks, "run_point", side_effect=_run_point),
        ):
            rc = job_entrypoint._run_apply_phase(
                self._cfg(), binary="tofu", var_file_argv=[], strip_dir=strip, child_grace=1.0
            )

        assert rc == 0
        up.assert_called_once()
        par.assert_called_once()  # apply succeeded + hook passed → result posted
        assert points == ["pre_apply", "post_apply"]

    def test_pre_apply_failure_errors_run_before_apply(self, tmp_path) -> None:
        from unittest.mock import patch

        from terrapod.runner.phases import execution_hooks as eh

        strip = tmp_path

        def _run_point(point, env=None):
            if point == "pre_apply":
                raise eh.HookError("pre_apply", "nope", 3)

        with (
            patch.object(job_entrypoint.plan_apply, "run_apply") as run_apply,
            patch.object(job_entrypoint.uploads, "upload_state") as up,
            patch.object(job_entrypoint.execution_hooks, "run_point", side_effect=_run_point),
        ):
            rc = job_entrypoint._run_apply_phase(
                self._cfg(), binary="tofu", var_file_argv=[], strip_dir=strip, child_grace=1.0
            )

        assert rc == 3, "pre_apply failure errors the run"
        run_apply.assert_not_called()  # nothing applied
        up.assert_not_called()  # no state touched


class TestPlanPhaseExecutionHooks:
    """post_plan hook semantics (#619): a post_plan hook must actually be
    invoked in the plan phase (regression for it being accepted+delivered but
    never executed), and a failing post_plan hook must error the run — which,
    for a plan+apply run, prevents the apply, mirroring an OPA denial. The plan
    artifacts are finalized (plan-result posted) BEFORE the gate runs, so the
    plan is visible in the UI regardless of the gate result."""

    def _cfg(self):
        from unittest.mock import MagicMock

        cfg = MagicMock()
        cfg.has_api = False  # skip lock-splice / plan-artifacts / upload blocks
        cfg.plan_only = False
        return cfg

    def _plan_result(self):
        from unittest.mock import MagicMock

        pr = MagicMock()
        pr.exit_code = 0
        pr.has_changes = True
        return pr

    def test_post_plan_hook_invoked_and_failure_errors_run(self, tmp_path) -> None:
        from unittest.mock import patch

        from terrapod.runner.phases import execution_hooks as eh

        points: list[str] = []

        def _run_point(point, env=None):
            points.append(point)
            if point == "post_plan":
                raise eh.HookError("post_plan", "gate denied", 7)

        with (
            patch.object(job_entrypoint.plan_apply, "run_plan", return_value=self._plan_result()),
            patch.object(job_entrypoint.opa, "evaluate_policies"),
            patch.object(job_entrypoint.uploads, "post_plan_result") as ppr,
            patch.object(job_entrypoint.uploads, "upload_plan_json"),
            patch.object(job_entrypoint.execution_hooks, "run_point", side_effect=_run_point),
        ):
            rc = job_entrypoint._run_plan_phase(
                self._cfg(), binary="tofu", var_file_argv=[], strip_dir=tmp_path, child_grace=1.0
            )

        assert rc == 7, "a failing post_plan hook errors the run with its exit code"
        assert "post_plan" in points, "post_plan hook must actually be invoked (#619 regression)"
        assert points == ["pre_plan", "post_plan"]
        ppr.assert_called_once()  # plan finalized/visible BEFORE the gate ran

    def test_post_plan_hook_success_returns_zero(self, tmp_path) -> None:
        from unittest.mock import patch

        points: list[str] = []

        def _run_point(point, env=None):
            points.append(point)

        with (
            patch.object(job_entrypoint.plan_apply, "run_plan", return_value=self._plan_result()),
            patch.object(job_entrypoint.opa, "evaluate_policies"),
            patch.object(job_entrypoint.uploads, "post_plan_result"),
            patch.object(job_entrypoint.uploads, "upload_plan_json"),
            patch.object(job_entrypoint.execution_hooks, "run_point", side_effect=_run_point),
        ):
            rc = job_entrypoint._run_plan_phase(
                self._cfg(), binary="tofu", var_file_argv=[], strip_dir=tmp_path, child_grace=1.0
            )

        assert rc == 0
        assert points == ["pre_plan", "post_plan"]


class TestThePulumiPhase:
    """#1523. The Pulumi branch is short, but every line of it is a thing that
    fails quietly when it is missing — a default backend, a default plugin host,
    or a `pulumi` picked up from the image rather than the cache."""

    def _cfg(self, monkeypatch):
        _env(monkeypatch)
        monkeypatch.setenv("TP_ENGINE", "pulumi")
        monkeypatch.setenv("TP_PULUMI_PHASE", "preview")
        from terrapod.runner.runner_config import RunnerConfig

        return RunnerConfig.from_env()

    def test_it_runs_the_binary_it_fetched(self, monkeypatch) -> None:
        """Not a bare `pulumi` off PATH. The runner image ships none, and one
        appearing there later would silently outrank the pinned version."""
        cfg = self._cfg(monkeypatch)
        with (
            patch(
                "terrapod.runner.phases.platform_tool.ensure_tool",
                return_value="/cache/pulumi",
            ),
            patch("terrapod.runner.exec_subprocess.run") as run,
        ):
            run.return_value.exit_code = 0
            assert job_entrypoint._run_pulumi_phase(cfg, child_grace=5) == 0

        assert run.call_args[0][0][0] == "/cache/pulumi"

    def test_it_fails_closed_when_the_binary_cannot_be_fetched(self, monkeypatch) -> None:
        """There is nothing to fall back to, and falling back to the name would
        either miss or run something unpinned."""
        cfg = self._cfg(monkeypatch)
        with (
            patch(
                "terrapod.runner.phases.platform_tool.ensure_tool",
                side_effect=RuntimeError("cache is down"),
            ),
            patch("terrapod.runner.exec_subprocess.run") as run,
        ):
            assert job_entrypoint._run_pulumi_phase(cfg, child_grace=5) == 1

        run.assert_not_called()

    def test_it_exports_the_backend_and_the_plugin_override(self, monkeypatch) -> None:
        """Both are read from the environment by the CLI, so the assertion is
        that they are actually in it by the time the subprocess is spawned."""
        cfg = self._cfg(monkeypatch)
        monkeypatch.delenv("PULUMI_BACKEND_URL", raising=False)
        monkeypatch.delenv("PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES", raising=False)

        seen = {}
        with (
            patch(
                "terrapod.runner.phases.platform_tool.ensure_tool",
                return_value="/cache/pulumi",
            ),
            patch("terrapod.runner.exec_subprocess.run") as run,
        ):
            run.side_effect = lambda *a, **k: seen.update(os.environ) or run.return_value
            run.return_value.exit_code = 0
            job_entrypoint._run_pulumi_phase(cfg, child_grace=5)

        assert seen["PULUMI_BACKEND_URL"].endswith("/api/terrapod/v1/pulumi")
        assert seen["PULUMI_PLUGIN_DOWNLOAD_URL_OVERRIDES"].startswith(".*=")

    def test_an_unknown_phase_is_refused_without_fetching_anything(self, monkeypatch) -> None:
        """The phase is checked first, so a bad one does not pay for a download
        it will discard."""
        cfg = self._cfg(monkeypatch)
        monkeypatch.setenv("TP_PULUMI_PHASE", "nonesuch")
        with patch("terrapod.runner.phases.platform_tool.ensure_tool") as ensure:
            assert job_entrypoint._run_pulumi_phase(cfg, child_grace=5) == 1
        ensure.assert_not_called()


class TestPulumiSkipsTerraformsSetup:
    """#1523. Where the Pulumi branch sits is load-bearing, not cosmetic.

    It used to sit at step 11, below `init` and the backend backstop, with a
    comment saying those steps "do not apply". They ran anyway, and the backstop
    ended the run: it raises when `.terraform/terraform.tfstate` is MISSING,
    which is what `tofu init` leaves in a directory holding a `Pulumi.yaml` and
    no `.tf` files. The run died before reaching Pulumi, advising the operator to
    remove a committed `override.tf` that did not exist.

    Asserted on the source rather than by driving the orchestrator, because the
    property is an ordering one and the alternative is mocking ten phases to
    observe which of them ran.
    """

    def _body_source(self) -> str:
        import inspect

        return inspect.getsource(job_entrypoint)

    def test_the_branch_precedes_init_and_the_backstop(self) -> None:
        src = self._body_source()
        branch = src.index('if os.environ.get("TP_ENGINE", "") == "pulumi":')

        for marker in ("# 9. Init", "# 10. Backend backstop", "# 8. Build var-file"):
            assert branch < src.index(marker), (
                f"the Pulumi branch must come before {marker!r} — those steps are "
                "Terraform's, and the backstop fails a Pulumi run outright"
            )

    def test_the_branch_follows_the_engine_neutral_setup(self) -> None:
        """Config tarball, chdir and the operator's pre_init hooks are not
        Terraform-specific, and a Pulumi run needs all three."""
        src = self._body_source()
        branch = src.index('if os.environ.get("TP_ENGINE", "") == "pulumi":')

        for marker in ("# 2. Configuration tarball", "# 4. Chdir", "# 7. pre_init"):
            assert branch > src.index(marker), (
                f"the Pulumi branch must come after {marker!r} — it is engine-neutral "
                "and Pulumi depends on it"
            )
