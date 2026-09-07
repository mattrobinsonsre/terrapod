"""Read cgroup v2 peak memory + CPU usage and POST to the API.

Port of `upload_resource_profile()` in docker/runner-entrypoint.sh
(#430). Called from the EXIT-trap equivalent in the Python
orchestrator — best-effort, never raises.

OOM-killed exits don't run user-space cleanup (SIGKILL is uncatchable),
so those cases are picked up by the listener reading the K8s pod's
terminated state. This module covers the clean-exit and signalled-exit
paths; both converge on the same DB columns.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import structlog

from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.resource_profile")


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def _read_cpu_usage_usec(stat_path: Path) -> int | None:
    """Parse `usage_usec` from /sys/fs/cgroup/cpu.stat."""
    try:
        text = stat_path.read_text()
    except (FileNotFoundError, OSError):
        return None
    for line in text.splitlines():
        if line.startswith("usage_usec "):
            try:
                return int(line.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                return None
    return None


def collect_profile(
    *,
    memory_peak_path: Path = Path("/sys/fs/cgroup/memory.peak"),
    cpu_stat_path: Path = Path("/sys/fs/cgroup/cpu.stat"),
    exit_code: int,
) -> dict[str, int]:
    """Read cgroup files. Returns a dict containing only the fields we
    actually read — the API treats missing fields as 'unknown'."""
    body: dict[str, int] = {"exit_code": exit_code}
    peak_mem = _read_int(memory_peak_path)
    if peak_mem is not None:
        body["peak_memory_bytes"] = peak_mem
    peak_cpu = _read_cpu_usage_usec(cpu_stat_path)
    if peak_cpu is not None:
        body["peak_cpu_usec"] = peak_cpu
    return body


def post_profile(
    cfg: RunnerConfig,
    exit_code: int,
    *,
    memory_peak_path: Path = Path("/sys/fs/cgroup/memory.peak"),
    cpu_stat_path: Path = Path("/sys/fs/cgroup/cpu.stat"),
    client: httpx.Client | None = None,
) -> bool:
    """POST the resource profile. Tight timeout (5s) — runner is
    exiting; we don't want to hang on a struggling API. Single attempt,
    no retry — best-effort.

    Returns True on 2xx; logs and returns False on any other outcome.
    Never raises (so the EXIT path stays graceful)."""
    if not cfg.has_api:
        return False

    body = collect_profile(
        memory_peak_path=memory_peak_path,
        cpu_stat_path=cpu_stat_path,
        exit_code=exit_code,
    )
    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/resource-profile"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0))

    try:
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            logger.info("resource-profile POST failed", err=str(exc))
            return False
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.info("resource-profile non-2xx", status=resp.status_code)
        return ok
    finally:
        if own_client:
            client.close()
