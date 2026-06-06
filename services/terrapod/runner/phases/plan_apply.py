"""plan + apply subprocess invocation.

Port of the `# --- Execute phase ---` block of
docker/runner-entrypoint.sh — the actual `tofu plan` / `tofu apply`
invocation. The signal forwarding + watchdog logic is already in
exec_subprocess (#445); this module just builds the argv and
interprets the result.

`detailed-exitcode` semantics for plan:
  0 = no changes (terraform's "no diff" path)
  1 = errored
  2 = changes present (apply would do something)

We normalise this to a (exit_code, has_changes) tuple for the
orchestrator's downstream OPA evaluation / plan-result POST.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from terrapod.runner import exec_subprocess
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.plan_apply")


@dataclass(frozen=True)
class PlanResult:
    exit_code: int
    has_changes: bool


def build_plan_argv(
    cfg: RunnerConfig,
    *,
    binary: str,
    var_file_args: list[str],
    plan_file: str = "tfplan",
) -> list[str]:
    argv = [binary, "plan", "-input=false", "-detailed-exitcode", f"-out={plan_file}"]
    if cfg.refresh_only:
        argv.append("-refresh-only")
    if not cfg.refresh:
        argv.append("-refresh=false")
    if cfg.destroy:
        argv.append("-destroy")
    argv.extend(var_file_args)
    for addr in cfg.target_addrs:
        argv.append(f"-target={addr}")
    # Plan-phase only: -replace.
    for addr in cfg.replace_addrs:
        argv.append(f"-replace={addr}")
    return argv


def build_apply_argv(
    cfg: RunnerConfig,
    *,
    binary: str,
    var_file_args: list[str],
    has_plan_file: bool,
    plan_file: str = "tfplan",
) -> list[str]:
    argv = [binary, "apply", "-input=false"]
    if has_plan_file:
        # When the binary plan file is present, var-files / -target are
        # already baked in — re-specifying them would error.
        argv.append(plan_file)
        return argv
    argv.append("-auto-approve")
    argv.extend(var_file_args)
    for addr in cfg.target_addrs:
        argv.append(f"-target={addr}")
    if cfg.allow_empty_apply:
        argv.append("-allow-empty-apply")
    return argv


def run_plan(
    cfg: RunnerConfig,
    *,
    binary: str,
    var_file_args: list[str],
    log_file: str,
    child_grace_seconds: float = 25.0,
) -> PlanResult:
    argv = build_plan_argv(cfg, binary=binary, var_file_args=var_file_args)
    logger.info("running plan", binary=binary)
    result = exec_subprocess.run(
        argv,
        log_file=log_file,
        child_grace_seconds=child_grace_seconds,
        tee_to_stdout=True,
    )
    if result.exit_code == 2:
        return PlanResult(exit_code=0, has_changes=True)
    if result.exit_code == 0:
        return PlanResult(exit_code=0, has_changes=False)
    # Errored — propagate the code, has_changes=False (won't be used)
    return PlanResult(exit_code=result.exit_code, has_changes=False)


def run_apply(
    cfg: RunnerConfig,
    *,
    binary: str,
    var_file_args: list[str],
    log_file: str,
    has_plan_file: bool,
    child_grace_seconds: float = 25.0,
) -> int:
    argv = build_apply_argv(
        cfg,
        binary=binary,
        var_file_args=var_file_args,
        has_plan_file=has_plan_file,
    )
    logger.info("running apply", binary=binary, has_plan_file=has_plan_file)
    result = exec_subprocess.run(
        argv,
        log_file=log_file,
        child_grace_seconds=child_grace_seconds,
        tee_to_stdout=True,
    )
    return result.exit_code


def run_plan_show_json(
    *,
    binary: str,
    plan_file: str = "tfplan",
    json_out: str = "/tmp/plan.json",
) -> bool:
    """Run `<binary> show -json tfplan` and write to json_out. Used by
    the OPA evaluation phase. Best-effort — returns True if the file
    has content, False otherwise."""
    import subprocess
    from pathlib import Path

    try:
        with Path(json_out).open("wb") as out:
            result = subprocess.run(  # noqa: S603
                [binary, "show", "-json", plan_file],
                check=False,
                stdout=out,
                stderr=subprocess.PIPE,
                timeout=120,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("show -json failed", err=str(exc))
        try:
            Path(json_out).unlink()
        except FileNotFoundError:
            pass
        return False

    if result.returncode != 0:
        logger.warning(
            "show -json returned non-zero",
            rc=result.returncode,
            stderr=result.stderr[:500].decode("utf-8", errors="replace"),
        )
        try:
            Path(json_out).unlink()
        except FileNotFoundError:
            pass
        return False

    return Path(json_out).exists() and Path(json_out).stat().st_size > 0
