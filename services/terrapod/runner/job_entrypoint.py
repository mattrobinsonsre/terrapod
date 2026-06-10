"""Entrypoint for runner K8s Jobs — pure Python orchestrator.

Owns the whole life of a single Terrapod run inside a Job pod:

  1. Parse env vars into RunnerConfig
  2. LogCapture wrap (combined log → uploaded on EVERY exit path)
  3. Download terraform/tofu binary (FATAL on miss)
  4. Download configuration tarball (FATAL on miss / extract failure)
  5. Download current state (best-effort)
  6. Apply-phase only: download plan-phase lock file
  7. Write terraform.rc (credentials + provider mirror + host redirect)
  8. Chdir into working subdirectory
  9. Setup script (if configured)
  10. Build var-file / target / replace argv pieces
  11. Run init (FATAL on non-zero)
  12. Backend backstop (FATAL if backend != local)
  13. Plan phase only: lock-file h1 injection + lock-file upload
  14. Plan: run plan; on success run show -json + OPA + plan-result
      + plan-file + plan-json upload
  15. Apply: download plan file (if exists); run apply; upload state
      (FATAL on state upload failure); apply-result

EXIT trap equivalent: a try/finally around the entire body uploads
the combined log + posts the resource profile no matter how we exit.

Successor to docker/runner-entrypoint.sh. Run as
`python -m terrapod.runner.job_entrypoint`. Returns the process exit
code; the K8s Job phase tracking maps any non-zero to "failed".
"""

from __future__ import annotations

import hashlib
import os
import signal
import sys
from pathlib import Path

import structlog

from terrapod.runner import lock_extender, plan_artifacts
from terrapod.runner.phases import (
    backend_backstop,
    init_phase,
    log_capture,
    mirror_config,
    opa,
    plan_apply,
    resource_profile,
    setup_script,
    tf_args,
    uploads,
    working_dir,
)
from terrapod.runner.phases.binary import BinaryDownloadError, download_binary
from terrapod.runner.phases.configuration import download_configuration
from terrapod.runner.phases.state import (
    download_plan_artifacts,
    download_state,
    reuse_plan_lock_file,
)
from terrapod.runner.runner_config import RunnerConfig

_DEFAULT_WORK_DIR = Path("/workspace")

_COMBINED_LOG = Path("/tmp/combined.log")
_INIT_LOG = Path("/tmp/init.log")
_PLAN_LOG = Path("/tmp/plan.log")
_APPLY_LOG = Path("/tmp/apply.log")
_PLAN_JSON = Path("/tmp/plan.json")
_TF_RC = Path("/tmp/terraform.rc")
_OPA_WORK = Path("/tmp/opa")

_UPLOAD_BUDGET_SECONDS = 25  # reserved for artifact uploads at end of grace
_MIN_CHILD_GRACE_SECONDS = 30


def _configure_stdio() -> None:
    """Make stdout/stderr line-buffered so each log line lands in
    `kubectl logs` promptly. PYTHONUNBUFFERED=1 in the Dockerfile makes
    the OS-level buffer unbuffered, but if the entrypoint is launched
    with the default block buffering (e.g. in a test harness, or when a
    future image change drops the env var) we still want line-by-line
    output. reconfigure(line_buffering=True) was added in Python 3.7
    and is a no-op if the stream is already line-buffered.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            # Wrapped (e.g. pytest capture) — best-effort only.
            pass


def _flush_stdio() -> None:
    """Flush stdout and stderr. Safe to call from any phase boundary;
    swallows errors so a broken pipe doesn't propagate."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass


def _configure_logging() -> None:
    """Human-readable console renderer — runner output goes straight
    into `kubectl logs` and the combined-log artifact. JSON would force
    operators to pipe everything through `jq` to read a plan."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        cache_logger_on_first_use=True,
    )


def _install_signal_logger() -> None:
    """Log SIGTERM/SIGQUIT receipt for visibility; the per-subprocess
    signal handler in exec_subprocess.run does the actual forwarding to
    the child. We DON'T re-raise — we want the Python orchestrator to
    keep running so the finally block uploads the log + resource
    profile."""

    def _handle(signum, frame):
        # Don't write to stdout from within a signal handler — the I/O
        # buffer might be mid-write from a subprocess tee. Just stash
        # that we saw it; the next user-space write will surface it.
        signal.signal(signum, signal.SIG_DFL)  # second signal → default
        log = structlog.get_logger("runner.job_entrypoint")
        log.info("received signal — graceful shutdown", signum=signum)

    for s in (signal.SIGTERM, signal.SIGQUIT):
        try:
            signal.signal(s, _handle)
        except (ValueError, OSError):
            pass


def _child_grace_seconds(cfg: RunnerConfig) -> float:
    grace = cfg.termination_grace_period_seconds - _UPLOAD_BUDGET_SECONDS
    if grace < _MIN_CHILD_GRACE_SECONDS:
        grace = _MIN_CHILD_GRACE_SECONDS
    return float(grace)


def _run_plan_phase(
    cfg: RunnerConfig,
    *,
    binary: str,
    var_file_argv: list[str],
    strip_dir: Path,
    child_grace: float,
) -> int:
    """Plan-phase body: optional lock-extender splice + lock-file upload,
    plan invocation, plan-json export, OPA gate, plan-result POST,
    plan-file + plan-json uploads.

    Returns the orchestrator's exit code (0 on success, non-zero on
    failure / OPA mandatory-set deny)."""
    log = structlog.get_logger("runner.job_entrypoint")

    # Lock-file h1 splice (best-effort) and lock-file upload. Done
    # BEFORE plan invocation so the apply phase's lock-file download
    # picks up the splice. The lock file lives at strip_dir.
    lock_path = strip_dir / ".terraform.lock.hcl"
    other_arch = lock_extender.detect_other_arch()
    if lock_path.exists() and other_arch and cfg.has_api:
        try:
            seen, extended = lock_extender.extend_lock_file(
                lock_path,
                api_url=cfg.api_url,
                auth_token=cfg.auth_token,
                other_arch=other_arch,
            )
            if seen - extended > 0:
                log.info(
                    "lock extender did not cover every provider — "
                    "falling back to `providers lock` for the rest",
                    seen=seen,
                    extended=extended,
                )
                # Best-effort fallback. We deliberately don't fail the
                # plan if this fails — apply may simply re-init on its
                # own arch.
                try:
                    import subprocess as _sp

                    _sp.run(  # noqa: S603
                        [binary, "providers", "lock", f"-platform={other_arch}"],
                        check=False,
                        timeout=300,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "providers lock fallback failed (non-fatal)",
                        err=str(exc),
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("lock extender raised (non-fatal)", err=str(exc))

    if lock_path.exists() and cfg.has_api:
        try:
            uploads.upload_lock_file(cfg, lock_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("lock-file upload raised (non-fatal)", err=str(exc))

    # Snapshot the workspace file tree AFTER init + lock-extender +
    # lock upload, BEFORE plan. The diff between this and the
    # post-plan snapshot is exactly the set of files plan generated
    # (data.archive_file outputs etc.) — uploaded as `plan-artifacts`
    # so apply can restore them.
    try:
        post_init_paths = plan_artifacts.snapshot_paths(strip_dir)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan-artifacts pre-plan snapshot failed (non-fatal)", err=str(exc))
        post_init_paths = None

    # Plan invocation.
    _PLAN_LOG.write_bytes(b"")
    _flush_stdio()
    plan_result = plan_apply.run_plan(
        cfg,
        binary=binary,
        var_file_args=var_file_argv,
        log_file=str(_PLAN_LOG),
        child_grace_seconds=child_grace,
    )
    _flush_stdio()
    if plan_result.exit_code != 0:
        log.warning("plan failed", rc=plan_result.exit_code)
        return plan_result.exit_code

    log.info("plan completed", has_changes=plan_result.has_changes)

    # Plan-artifacts: ALWAYS upload a tar, even when the diff is empty
    # (`tar_files` writes a 1024-byte EOF-only archive in that case).
    # An always-present upload lets apply treat a download 404 as a
    # real "something is wrong" signal rather than the ambiguous
    # "either no diff OR the runner that ran plan didn't have this
    # feature".
    if post_init_paths is not None and cfg.has_api:
        try:
            post_plan_paths = plan_artifacts.snapshot_paths(strip_dir)
            new_files = plan_artifacts.compute_diff(post_init_paths, post_plan_paths)
            import tempfile as _tempfile

            fd, tmp = _tempfile.mkstemp(suffix=".plan-artifacts.tar")
            os.close(fd)
            tmp_path = Path(tmp)
            try:
                size = plan_artifacts.tar_files(strip_dir, new_files, tmp_path)
                log.info(
                    "plan-artifacts tar built",
                    files=len(new_files),
                    bytes=size,
                    empty=not new_files,
                )
                uploads.upload_plan_artifacts(cfg, tmp_path)
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plan-artifacts upload pipeline raised (non-fatal)",
                err=str(exc),
            )

    # Export show -json (for OPA + UI artifact). Best-effort.
    plan_show_ok = False
    if Path("tfplan").exists():
        plan_show_ok = plan_apply.run_plan_show_json(
            binary=binary, plan_file="tfplan", json_out=str(_PLAN_JSON)
        )

    # OPA evaluation. Mandatory-set denials → non-zero exit.
    try:
        opa.evaluate_policies(
            cfg,
            plan_json=_PLAN_JSON if plan_show_ok else _PLAN_JSON,
            work_dir=_OPA_WORK,
        )
    except opa.PolicyEvaluationError as exc:
        log.error("policy evaluation failed", err=str(exc))
        return 1

    # plan-result POST.
    try:
        uploads.post_plan_result(cfg, has_changes=plan_result.has_changes)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan-result raised (non-fatal)", err=str(exc))

    # Plan binary upload (skip for plan-only).
    if Path("tfplan").exists() and not cfg.plan_only:
        try:
            uploads.upload_plan_file(cfg, Path("tfplan"))
        except Exception as exc:  # noqa: BLE001
            log.warning("plan-file upload raised (non-fatal)", err=str(exc))

    # Plan JSON upload (best-effort).
    if _PLAN_JSON.exists() and _PLAN_JSON.stat().st_size > 0:
        try:
            uploads.upload_plan_json(cfg, _PLAN_JSON)
        except Exception as exc:  # noqa: BLE001
            log.warning("plan-json upload raised (non-fatal)", err=str(exc))

    return 0


def _state_digest(state_path: Path) -> str | None:
    """SHA-256 of the state file, or None if it doesn't exist. Used to
    detect a serial-neutral no-op apply: tofu/terraform does NOT rewrite
    the state with a bumped serial when the persisted state is unchanged
    (e.g. a resource with a perpetual phantom diff — write-only attributes
    re-sent every apply). In that case the post-apply state is byte-identical
    to the state we downloaded, so there is nothing to upload — and uploading
    it would collide with the existing serial and be mis-flagged as a state
    divergence."""
    try:
        if state_path.exists() and state_path.stat().st_size > 0:
            return hashlib.sha256(state_path.read_bytes()).hexdigest()
    except OSError:
        return None
    return None


def _run_apply_phase(
    cfg: RunnerConfig,
    *,
    binary: str,
    var_file_argv: list[str],
    strip_dir: Path,
    child_grace: float,
) -> int:
    """Apply-phase body: download plan binary, run apply, upload state
    (FATAL on failure), apply-result POST."""
    log = structlog.get_logger("runner.job_entrypoint")

    # Snapshot the pre-apply state so we can tell whether apply actually
    # changed it. tofu overwrites terraform.tfstate in place, so a digest
    # taken now (the state we downloaded) versus after apply tells us if
    # the serial was bumped. See _state_digest for why this matters.
    pre_apply_state_digest = _state_digest(strip_dir / "terraform.tfstate")

    # Download plan file from plan phase. Best-effort — when missing,
    # apply re-plans inline. Routed through `download_to_file` so the
    # redirect-hostname-rewrite logic kicks in: filesystem-backend
    # storage emits presigned URLs at the deployment's public hostname
    # (e.g. terrapod.local in dev), which the runner pod can't reach
    # from inside the cluster. `download_to_file` rewrites those back
    # to TP_API_URL for the /api/terrapod/v1/storage/ path prefix.
    # Cloud-backend redirects (S3 / GCS / Azure) are passed through
    # untouched. Using raw `httpx.get(follow_redirects=True)` here used
    # to silently 502 in Tilt because the filesystem URL is
    # unreachable from inside the cluster.
    plan_file = strip_dir / "tfplan"
    has_plan_file = False
    if cfg.has_api:
        from terrapod.runner.download import download_to_file as _download_to_file

        headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
        result = _download_to_file(
            f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/plan-file",
            plan_file,
            headers=headers,
            api_url=cfg.api_url,
            retries=cfg.download_retries,
            retry_delay_seconds=cfg.download_retry_delay_seconds,
        )
        if result.ok and plan_file.exists() and plan_file.stat().st_size > 0:
            has_plan_file = True
        else:
            plan_file.unlink(missing_ok=True)
            log.info("plan-file not available", status=result.status)

    # Download + extract the plan-phase workspace-diff tarball over the
    # initialised workspace. This restores files plan generated but
    # apply's fresh-pod init doesn't reproduce (`data.archive_file`
    # outputs, `null_resource` local-exec scratch, etc.).
    #
    # Transition contract: plan phase always uploads (even an empty
    # tar) starting at the version that introduced this feature, so a
    # download 404 is a real "where's the plan?" signal in steady
    # state. But during the rollout window an apply may run against a
    # plan that ran on a pre-feature runner image and never uploaded.
    # Log loudly but DO NOT abort — let tofu apply hit the original
    # "no such file or directory" error on whichever resource needs
    # the missing file. That gives operators a clear, in-context
    # diagnostic instead of a generic pre-flight refusal, and once
    # the upgrade window closes the 404 becomes vanishingly rare.
    if cfg.has_api and has_plan_file:
        import tempfile as _tempfile

        fd, tmp = _tempfile.mkstemp(suffix=".plan-artifacts.tar")
        os.close(fd)
        tar_path = Path(tmp)
        try:
            ok = download_plan_artifacts(cfg, dest=tar_path)
            if not ok:
                log.error(
                    "plan-artifacts tarball not available — apply will "
                    "proceed but any resource that depends on a plan-time "
                    "generated file (data.archive_file output, "
                    "null_resource local-exec scratch, etc.) will fail "
                    "at tofu time with a 'no such file or directory' "
                    "error. This is expected during the rollout window "
                    "for plans that ran on a pre-feature runner image."
                )
            else:
                try:
                    extracted = plan_artifacts.extract_over(tar_path, strip_dir)
                    log.info("plan-artifacts extracted", files=extracted)
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "plan-artifacts extract failed; apply will "
                        "proceed without restore (see above for "
                        "the resource-level error this will produce)",
                        err=str(exc),
                    )
        finally:
            try:
                tar_path.unlink(missing_ok=True)
            except OSError:
                pass

    _APPLY_LOG.write_bytes(b"")
    _flush_stdio()
    rc = plan_apply.run_apply(
        cfg,
        binary=binary,
        var_file_args=var_file_argv,
        log_file=str(_APPLY_LOG),
        has_plan_file=has_plan_file,
        child_grace_seconds=child_grace,
    )
    _flush_stdio()
    if rc != 0:
        log.warning("apply failed", rc=rc)

    # State upload — FATAL if non-empty state file exists and upload
    # fails. Apply may have written state even on a failed apply (partial
    # apply). Always try to upload if the file exists.
    #
    # ...UNLESS the apply was serial-neutral: if the post-apply state is
    # byte-identical to the state we downloaded, tofu did not bump the
    # serial (a no-op apply driven by a perpetual phantom diff). Re-uploading
    # it would collide with the already-recorded serial and be mis-flagged as
    # a state divergence. There is nothing to persist — the API already holds
    # this exact state — so skip the upload cleanly. Only skip on a successful
    # apply (rc == 0); a failed/partial apply must still try to upload whatever
    # state was written.
    state_path = strip_dir / "terraform.tfstate"
    if (
        rc == 0
        and pre_apply_state_digest is not None
        and _state_digest(state_path) == pre_apply_state_digest
    ):
        log.info("state unchanged after apply — skipping upload (serial-neutral no-op)")
    elif state_path.exists() and state_path.stat().st_size > 0:
        try:
            ok = uploads.upload_state(cfg, state_path)
            if not ok:
                # signal_state_diverged and force a non-zero exit.
                try:
                    uploads.signal_state_diverged(cfg)
                except Exception as exc:  # noqa: BLE001
                    log.warning("state-diverged signal raised", err=str(exc))
                if rc == 0:
                    rc = 1
        except Exception as exc:  # noqa: BLE001
            log.error("state upload raised — flagging diverged", err=str(exc))
            try:
                uploads.signal_state_diverged(cfg)
            except Exception:  # noqa: BLE001, S110
                pass
            if rc == 0:
                rc = 1

    if rc == 0:
        try:
            uploads.post_apply_result(cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("apply-result raised (non-fatal)", err=str(exc))

    return rc


def _run_body(cfg: RunnerConfig, work_dir: Path) -> int:
    """Drive every phase. Caller wraps this in try/finally for log +
    profile upload."""
    log = structlog.get_logger("runner.job_entrypoint")

    # 1. Binary cache download. (download_binary itself logs "binary
    # ready" on success — no need to repeat it here.)
    binary_path = download_binary(cfg)
    binary = str(binary_path)

    # 2. Configuration tarball.
    work_dir.mkdir(parents=True, exist_ok=True)
    config_result = download_configuration(cfg, work_dir=work_dir)
    strip_dir = config_result.strip_dir

    # 3. Mirror config + TF_CLI_CONFIG_FILE. Sets env vars on os.environ
    # so subsequent subprocess invocations inherit them.
    mirror_config.write_terraform_rc(
        api_url=cfg.api_url,
        auth_token=cfg.auth_token,
        public_api_url=cfg.public_api_url,
        config_path=_TF_RC,
    )
    for k, v in mirror_config.export_env(config_path=_TF_RC, env={}).items():
        os.environ[k] = v

    # 4. Chdir into working subdirectory.
    cwd = working_dir.resolve_and_chdir(strip_dir, cfg.working_dir)
    log.info("chdir", cwd=str(cwd))

    # 5. State download — AFTER chdir so terraform.tfstate lands beside
    # the user's .tf files.
    state_present = download_state(cfg, strip_dir=cwd)
    if state_present:
        log.info("state file present after download")

    # 6. Apply-phase: try to reuse plan-phase lock file.
    if cfg.phase == "apply":
        reuse_plan_lock_file(cfg, strip_dir=cwd)

    # 7. Setup script (operator-supplied tfvars / cloud auth / etc.).
    try:
        setup_script.run(cfg.setup_script, env=os.environ.copy())
    except setup_script.SetupScriptError as exc:
        log.error("setup script failed", rc=exc.exit_code)
        return exc.exit_code

    # 8. Build var-file / target / replace argv pieces.
    var_file_argv = tf_args.var_file_args(cfg.var_files)

    # 9. Init. Apply-phase init runs with -lockfile=readonly so it
    # doesn't drop the other-arch hashes the plan phase's lock_extender
    # spliced in (tofu's network_mirror code path only records hashes
    # for the target platform — see init_phase.run_init docstring).
    # Plan phase runs init normally so the splice has a complete
    # base lock to extend.
    child_grace = _child_grace_seconds(cfg)
    _flush_stdio()
    try:
        init_phase.run_init(
            binary=binary,
            var_file_args=var_file_argv,
            log_file=str(_INIT_LOG),
            child_grace_seconds=child_grace,
            lockfile_readonly=(cfg.phase == "apply"),
        )
    except init_phase.InitError as exc:
        log.error("init failed", rc=exc.exit_code)
        _flush_stdio()
        return exc.exit_code
    _flush_stdio()

    # 10. Backend backstop.
    try:
        backend_backstop.verify_local_backend(cwd)
    except backend_backstop.BackendBackstopError as exc:
        log.error("backend backstop failed", err=str(exc))
        return 1

    # 11. Phase-specific execution.
    if cfg.phase == "plan":
        return _run_plan_phase(
            cfg,
            binary=binary,
            var_file_argv=var_file_argv,
            strip_dir=cwd,
            child_grace=child_grace,
        )
    if cfg.phase == "apply":
        return _run_apply_phase(
            cfg,
            binary=binary,
            var_file_argv=var_file_argv,
            strip_dir=cwd,
            child_grace=child_grace,
        )
    log.error("unknown phase", phase=cfg.phase)
    return 1


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    _configure_logging()
    _install_signal_logger()
    log = structlog.get_logger("runner.job_entrypoint")

    work_dir_env = os.environ.get("WORK_DIR")
    work_dir = Path(work_dir_env) if work_dir_env else _DEFAULT_WORK_DIR

    cfg = RunnerConfig.from_env()
    # Phase-aware artifact name: combined log uploads to {phase}-log.
    log_artifact_phase = cfg.phase

    exit_code = 1
    with log_capture.LogCapture(_COMBINED_LOG) as combined:
        try:
            exit_code = _run_body(cfg, work_dir)
        except BinaryDownloadError as exc:
            log.error("binary download failed", err=str(exc))
            exit_code = 1
        except SystemExit as exc:
            exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001
            log.exception("orchestrator crashed", err=str(exc))
            exit_code = 1
        finally:
            # Roll per-phase logs into combined before uploading.
            for f in (_INIT_LOG, _PLAN_LOG, _APPLY_LOG):
                combined.append_file(f)
            # Flush stdout/stderr so anything buffered before the
            # LogCapture handle closes is visible in `kubectl logs`.
            _flush_stdio()

    # Best-effort: combined log and resource profile uploads. These
    # happen AFTER LogCapture exits so its file handle is flushed.
    try:
        log_capture.upload_combined_log(cfg, _COMBINED_LOG, phase=log_artifact_phase)
    except Exception as exc:  # noqa: BLE001
        log.warning("combined log upload raised", err=str(exc))

    try:
        resource_profile.post_profile(cfg, exit_code=exit_code)
    except Exception as exc:  # noqa: BLE001
        log.warning("resource profile post raised", err=str(exc))

    log.info("phase complete", phase=cfg.phase, exit_code=exit_code)
    _flush_stdio()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
