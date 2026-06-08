"""Artifact upload + run-result POST helpers for the runner.

Port of the curl-based upload blocks scattered through the plan / apply
phases of docker/runner-entrypoint.sh. Each helper:

  * Builds the right URL from a RunnerConfig.
  * Streams the file payload (PUT, application/octet-stream) or posts
    a small JSON body.
  * Logs the outcome.
  * Returns a bool for the caller to decide policy: lock-file +
    plan-file + plan-json-output are BEST-EFFORT (a failure produces a
    warning); state upload is FATAL — its caller signals
    `state-diverged` and exits non-zero.

The CLI at the bottom (run via `python -m terrapod.runner.upload_cli`)
makes these callable from the bash continuation without bash needing
to inline curl flags or JSON bodies.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import structlog

from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.uploads")


def _client_for(cfg: RunnerConfig) -> httpx.Client:
    """Builds an httpx.Client sized for large file uploads. Connect
    timeout stays tight (10s); read+write+pool match the bash
    TP_UPLOAD_TIMEOUT so a stalled upload doesn't burn the runner's
    grace budget.
    """
    return httpx.Client(
        timeout=httpx.Timeout(
            float(cfg.upload_timeout_seconds),
            connect=10.0,
        ),
        headers={"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {},
    )


def _put_file(
    cfg: RunnerConfig,
    url: str,
    path: Path,
    *,
    content_type: str = "application/octet-stream",
    client: httpx.Client | None = None,
) -> tuple[bool, int | None]:
    """PUT a file as the request body. Returns (ok, status)."""
    if not cfg.has_api:
        return False, None
    if not path.exists() or path.stat().st_size == 0:
        logger.info("file missing or empty — skipping upload", path=str(path))
        return False, None

    own_client = client is None
    if client is None:
        client = _client_for(cfg)

    try:
        with path.open("rb") as fh:
            resp = client.put(url, content=fh.read(), headers={"Content-Type": content_type})
        ok = 200 <= resp.status_code < 300
        return ok, resp.status_code
    except httpx.RequestError as exc:
        logger.warning("upload request failed", url=url, err=str(exc))
        return False, None
    finally:
        if own_client:
            client.close()


def _post_json(
    cfg: RunnerConfig,
    url: str,
    body: dict | None = None,
    *,
    timeout_seconds: float = 10.0,
    client: httpx.Client | None = None,
) -> tuple[bool, int | None]:
    if not cfg.has_api:
        return False, None
    own_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers={"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {},
        )
    try:
        if body is None:
            resp = client.post(url)
        else:
            resp = client.post(url, json=body)
        ok = 200 <= resp.status_code < 300
        return ok, resp.status_code
    except httpx.RequestError as exc:
        logger.warning("post request failed", url=url, err=str(exc))
        return False, None
    finally:
        if own_client:
            client.close()


# ── Upload helpers ────────────────────────────────────────────────────


def upload_lock_file(
    cfg: RunnerConfig,
    lock_path: Path,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Plan phase. Best-effort. Failure means the apply phase resolves
    providers independently — drift risk but not fatal."""
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/lock-file"
    ok, status = _put_file(cfg, url, lock_path, client=client)
    if ok:
        logger.info("uploaded .terraform.lock.hcl for apply-phase reuse")
    else:
        logger.warning(
            "lock file upload non-OK; apply phase will resolve providers independently (non-fatal)",
            status=status,
        )
    return ok


def upload_plan_file(
    cfg: RunnerConfig,
    plan_path: Path,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Plan phase, non-plan-only. The apply phase downloads this binary
    plan file. Best-effort — a failure means apply re-plans from
    scratch."""
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/plan-file"
    ok, status = _put_file(cfg, url, plan_path, client=client)
    if not ok:
        logger.warning("plan-file upload failed (non-fatal)", status=status)
    return ok


def upload_plan_json(
    cfg: RunnerConfig,
    json_path: Path,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Plan phase. Best-effort. The UI serves this for the AI-summary
    and structured plan display; if it's missing the relevant read
    endpoint just 404s."""
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/plan-json-output"
    ok, status = _put_file(
        cfg,
        url,
        json_path,
        content_type="application/json",
        client=client,
    )
    if not ok:
        logger.warning("plan-json-output upload failed (non-fatal)", status=status)
    return ok


def upload_state(
    cfg: RunnerConfig,
    state_path: Path,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Apply phase. FATAL on failure — caller is expected to signal
    state-diverged and exit non-zero. The runner already wrote state
    locally; if we can't upload it, the workspace's reality and the
    API's idea of state have diverged."""
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/state"
    ok, status = _put_file(cfg, url, state_path, client=client)
    if not ok:
        logger.error(
            "FATAL: state upload failed — caller will flag state-diverged",
            status=status,
        )
    return ok


def signal_state_diverged(
    cfg: RunnerConfig,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """POST to flag the workspace as state-diverged. Best-effort, tight
    timeout — we've already exited with a fatal status; this is just to
    surface the state divergence in the UI."""
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/state-diverged"
    ok, status = _post_json(cfg, url, body=None, timeout_seconds=5.0, client=client)
    if not ok:
        logger.warning("state-diverged signal POST failed", status=status)
    return ok


def post_plan_result(
    cfg: RunnerConfig,
    *,
    has_changes: bool,
    client: httpx.Client | None = None,
) -> bool:
    """Plan phase. Best-effort. The reconciler's listener path is the
    fallback; this just drives the planning→planned transition faster
    when the runner can reach the API directly."""
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/plan-result"
    ok, status = _post_json(
        cfg,
        url,
        body={"has_changes": has_changes},
        client=client,
    )
    if not ok:
        logger.warning("plan-result POST failed (non-fatal)", status=status)
    return ok


def post_apply_result(
    cfg: RunnerConfig,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """Apply phase. Best-effort. Drives applying→applied transition
    without the listener round-trip."""
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/apply-result"
    ok, status = _post_json(cfg, url, body=None, client=client)
    if not ok:
        logger.warning("apply-result POST failed (non-fatal)", status=status)
    return ok


# ── CLI for bash invocation ───────────────────────────────────────────


def _cli_main(argv: list[str] | None = None) -> int:
    """Subcommand-style CLI so the bash entrypoint can do
    `python -m terrapod.runner.upload_cli plan-file ./tfplan` per
    upload site without inlining curl flags."""
    import argparse
    import sys

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        cache_logger_on_first_use=True,
    )

    parser = argparse.ArgumentParser(prog="python -m terrapod.runner.upload_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_lock = sub.add_parser("lock-file", help="Upload .terraform.lock.hcl")
    p_lock.add_argument("path")

    p_plan = sub.add_parser("plan-file", help="Upload binary plan file")
    p_plan.add_argument("path")

    p_pjson = sub.add_parser("plan-json", help="Upload JSON plan output")
    p_pjson.add_argument("path")

    p_state = sub.add_parser("state", help="Upload terraform.tfstate (FATAL)")
    p_state.add_argument("path")

    sub.add_parser("state-diverged", help="Signal state divergence")

    p_pres = sub.add_parser("plan-result", help="POST plan-result")
    p_pres.add_argument(
        "--has-changes",
        choices=("true", "false"),
        required=True,
    )

    sub.add_parser("apply-result", help="POST apply-result")

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])
    cfg = RunnerConfig.from_env()

    if ns.cmd == "lock-file":
        ok = upload_lock_file(cfg, Path(ns.path))
    elif ns.cmd == "plan-file":
        ok = upload_plan_file(cfg, Path(ns.path))
    elif ns.cmd == "plan-json":
        ok = upload_plan_json(cfg, Path(ns.path))
    elif ns.cmd == "state":
        ok = upload_state(cfg, Path(ns.path))
        if not ok:
            signal_state_diverged(cfg)
            return 1
    elif ns.cmd == "state-diverged":
        ok = signal_state_diverged(cfg)
    elif ns.cmd == "plan-result":
        ok = post_plan_result(cfg, has_changes=(ns.has_changes == "true"))
    elif ns.cmd == "apply-result":
        ok = post_apply_result(cfg)
    else:
        return 2

    # Best-effort uploads still return 0 here; we just log the warning.
    # The only command that returns non-zero on failure is `state`,
    # because state divergence is fatal.
    _ = ok
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli_main())
