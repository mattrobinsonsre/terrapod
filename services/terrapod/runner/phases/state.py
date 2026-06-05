"""Phases: download the current state (every run) and re-use the
plan-phase lock file (apply phase only).

Ports of the `# --- Download current state ---` and `# --- Apply
phase: try to reuse the plan-phase lock file ---` blocks of
docker/runner-entrypoint.sh (~lines 754–782 in the v0.31.x tree).

Both are best-effort — a 404 means there's nothing to download (first
run, no state yet; plan didn't upload a lock file) which the next
phase tolerates. Hard storage errors are logged but don't fail the
run; the bash version did the same (`|| true`).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import structlog

from terrapod.runner.download import download_to_file
from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.phase.state")


def download_state(
    cfg: RunnerConfig,
    *,
    strip_dir: Path,
    client: httpx.Client | None = None,
) -> bool:
    """Pull the workspace's current state into the strip_dir.

    Returns True iff a file was written. False just means there's no
    state yet (first run) or the API isn't reachable in a dev/test
    invocation. Both cases are non-fatal — `tofu plan` against an
    empty state is what creates the first state version.
    """
    if not cfg.has_api:
        return False

    state_file = strip_dir / "terraform.tfstate"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    logger.info("downloading current state", run_id=cfg.run_id)
    result = download_to_file(
        f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/state",
        state_file,
        headers=headers,
        api_url=cfg.api_url,
        retries=cfg.download_retries,
        retry_delay_seconds=cfg.download_retry_delay_seconds,
        client=client,
    )

    if not result.ok:
        # 404 is the common case (first run). Don't warn-spam.
        if result.status == 404:
            logger.info("no prior state — assumed first run")
        else:
            logger.warning("state download non-ok", status=result.status)
        state_file.unlink(missing_ok=True)
        return False

    return True


def reuse_plan_lock_file(
    cfg: RunnerConfig,
    *,
    strip_dir: Path,
    client: httpx.Client | None = None,
) -> bool:
    """Apply phase: re-download the .terraform.lock.hcl the plan phase
    uploaded so apply-init resolves to the same provider versions
    (#306). Without this, init re-evaluates the version constraint and
    may pick up a newer version published in the plan→apply window.

    Best-effort. Returns True if the lock file was successfully
    fetched; the orchestrator just logs and continues if not.
    """
    if cfg.phase != "apply" or not cfg.has_api:
        return False

    lock_file = strip_dir / ".terraform.lock.hcl"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    result = download_to_file(
        f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/lock-file",
        lock_file,
        headers=headers,
        api_url=cfg.api_url,
        retries=cfg.download_retries,
        retry_delay_seconds=cfg.download_retry_delay_seconds,
        client=client,
    )

    if not result.ok:
        lock_file.unlink(missing_ok=True)
        logger.info(
            "no plan-phase lock file available; apply init will resolve providers independently",
            status=result.status,
        )
        return False

    logger.info("reusing .terraform.lock.hcl from plan phase")
    return True
