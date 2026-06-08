"""Combined-log capture for guaranteed log upload on every exit path.

Port of `COMBINED_LOG`, `log()`, `upload_log()`, and the `on_exit`
EXIT trap in docker/runner-entrypoint.sh.

The bash entrypoint accumulates every script line into /tmp/combined.log
and an EXIT trap uploads it on every exit path (clean success, init
failure, plan failure, SIGTERM, early failure). The Python orchestrator
takes over that responsibility:

  * `LogCapture` tees Python's stdout/stderr to /tmp/combined.log.
    Subprocess output (init, plan, apply) is appended via the existing
    exec_subprocess.run(log_file=...) parameter — every phase writes
    to a file under /tmp/, and the orchestrator concatenates each into
    the combined log after the phase runs.
  * `upload_combined_log(cfg, phase_name)` PUTs the file to
    /artifacts/{phase}-log. Linear backoff, 3 attempts; FATAL marker
    to stderr (visible via kubectl logs) on final failure. Always
    returns gracefully so the EXIT path stays clean.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import structlog

from terrapod.runner.runner_config import RunnerConfig

logger = structlog.get_logger("runner.log_capture")


class LogCapture:
    """Tee `sys.stdout` and `sys.stderr` to a combined log file.

    Used as a context manager:

        with LogCapture(Path("/tmp/combined.log")) as combined:
            ...phases run...
            combined.append_file(Path("/tmp/plan.log"))

    Subprocess phases that already write to their own log file pass it
    to `append_file` after the phase finishes — the bash version did
    the same via `cat plan.log >> COMBINED_LOG`.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None
        self._orig_stdout_write = None
        self._orig_stderr_write = None

    def __enter__(self) -> LogCapture:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate at start so re-runs don't append to a stale log.
        self._fh = self.path.open("wb")

        # Tee stdout/stderr writes. We patch the .write method instead
        # of replacing sys.stdout because Python's structlog and the
        # bare print()s in the entrypoint both go through .write — and
        # leaving sys.stdout intact preserves the buffer semantics
        # subprocess.Popen relies on.
        self._orig_stdout_write = sys.stdout.write
        self._orig_stderr_write = sys.stderr.write

        def tee_stdout(s: str) -> int:
            n = self._orig_stdout_write(s)
            # Flush the live stream so each log line reaches kubectl
            # logs immediately. structlog's JSONRenderer + bare print
            # both end here; without this the line sits in the stream's
            # block buffer if PYTHONUNBUFFERED isn't set in the env.
            try:
                sys.stdout.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                self._fh.write(s.encode("utf-8", errors="replace"))  # type: ignore[union-attr]
                self._fh.flush()  # type: ignore[union-attr]
            except (OSError, AttributeError):
                pass
            return n

        def tee_stderr(s: str) -> int:
            n = self._orig_stderr_write(s)
            try:
                sys.stderr.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                self._fh.write(s.encode("utf-8", errors="replace"))  # type: ignore[union-attr]
                self._fh.flush()  # type: ignore[union-attr]
            except (OSError, AttributeError):
                pass
            return n

        sys.stdout.write = tee_stdout  # type: ignore[method-assign]
        sys.stderr.write = tee_stderr  # type: ignore[method-assign]
        return self

    def __exit__(self, *args) -> None:
        if self._orig_stdout_write is not None:
            sys.stdout.write = self._orig_stdout_write  # type: ignore[method-assign]
        if self._orig_stderr_write is not None:
            sys.stderr.write = self._orig_stderr_write  # type: ignore[method-assign]
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass

    def append_file(self, src: Path) -> None:
        """Append a file's bytes to the combined log. Used after each
        subprocess phase (init.log, plan.log, apply.log) to roll the
        per-phase log into the combined upload."""
        if not src.exists() or self._fh is None:
            return
        try:
            with src.open("rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self._fh.write(chunk)
            self._fh.flush()
        except OSError as exc:
            logger.info("append_file failed", src=str(src), err=str(exc))


def upload_combined_log(
    cfg: RunnerConfig,
    combined_path: Path,
    *,
    phase: str,
    client: httpx.Client | None = None,
    sleep: callable = time.sleep,
) -> bool:
    """PUT the combined log to /artifacts/{phase}-log with linear
    backoff (3 attempts, sleep 2/4 seconds). Best-effort: always
    returns gracefully so the EXIT path can continue. Writes a FATAL
    marker to stderr on final failure so it's visible via kubectl logs.
    """
    if not cfg.has_api or not combined_path.exists() or combined_path.stat().st_size == 0:
        return False

    url = f"{cfg.api_url}/api/terrapod/v1/runs/{cfg.run_id}/artifacts/{phase}-log"
    headers = {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        with combined_path.open("rb") as f:
            data = f.read()
        for attempt in range(1, 4):
            try:
                resp = client.put(
                    url,
                    content=data,
                    headers={"Content-Type": "application/octet-stream", **headers},
                )
                if 200 <= resp.status_code < 300:
                    return True
            except httpx.RequestError:
                pass
            if attempt < 3:
                sleep(attempt * 2)
        # Final FATAL marker for kubectl logs.
        size = combined_path.stat().st_size
        sys.stderr.write(
            f"[entrypoint] FATAL: log upload failed after 3 attempts "
            f"(artifact={phase}-log, size={size}B, run={cfg.run_id})\n"
        )
        return False
    finally:
        if own_client:
            client.close()
