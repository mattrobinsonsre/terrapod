"""Runner listener main loop.

Entrypoint: python -m terrapod.runner.listener

The listener:
1. Establishes identity (join pool via token exchange)
2. Starts heartbeat loop (every 60s)
3. Polls for queued runs (every 5s)
4. Spawns K8s Jobs for claimed runs
5. Watches Jobs to completion and reports status back
"""

import asyncio
import base64
import os
import signal
import time
from urllib.parse import urlparse, urlunparse

import httpx

from terrapod.config import load_runner_config
from terrapod.logging_config import configure_logging, get_logger

logger = get_logger(__name__)

# Shutdown flag
_shutdown = asyncio.Event()


class RunnerListener:
    """Main listener controller — ARC-pattern Job controller."""

    def __init__(self):
        self.identity = None
        self.runner_config = load_runner_config()
        self.active_tasks: dict[str, asyncio.Task] = {}  # run_id → task
        self._poll_interval = int(os.environ.get("TERRAPOD_POLL_INTERVAL", "5"))
        self._heartbeat_interval = int(os.environ.get("TERRAPOD_HEARTBEAT_INTERVAL", "60"))
        self._max_concurrent = int(os.environ.get("TERRAPOD_MAX_CONCURRENT", "3"))

    def _auth_headers(self) -> dict[str, str]:
        """Build authentication headers for API calls."""
        headers = {}
        if self.identity.certificate_pem:
            cert_b64 = base64.b64encode(self.identity.certificate_pem.encode()).decode()
            headers["X-Terrapod-Client-Cert"] = cert_b64
        return headers

    async def start(self) -> None:
        """Main entry point — initialize and start loops."""
        from terrapod.runner.identity import establish_identity
        from terrapod.runner.job_manager import init_k8s

        init_k8s()

        # Establish identity via join token
        self.identity = await establish_identity()
        logger.info(
            "Listener started",
            listener_id=str(self.identity.listener_id),
            name=self.identity.name,
        )

        # Start concurrent loops
        await asyncio.gather(
            self._heartbeat_loop(),
            self._poll_loop(),
            self._shutdown_waiter(),
        )

    async def _shutdown_waiter(self) -> None:
        """Wait for shutdown signal."""
        await _shutdown.wait()
        logger.info("Shutdown signal received, draining active tasks...")

        # Cancel active tasks
        for run_id, task in self.active_tasks.items():
            if not task.done():
                logger.info("Cancelling active run", run_id=run_id)
                task.cancel()

        # Wait for tasks to finish (with timeout)
        if self.active_tasks:
            done, pending = await asyncio.wait(
                self.active_tasks.values(),
                timeout=120,
            )
            for task in pending:
                task.cancel()

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat to API every 60 seconds."""
        while not _shutdown.is_set():
            try:
                await self._send_heartbeat()
            except Exception as e:
                logger.error("Heartbeat failed", error=str(e))

            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=self._heartbeat_interval)
                return  # Shutdown signaled
            except TimeoutError:
                pass

    async def _send_heartbeat(self) -> None:
        """Send heartbeat via API."""
        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=10) as client:
            await client.post(
                f"/api/v2/listeners/listener-{self.identity.listener_id}/heartbeat",
                json={
                    "capacity": self._max_concurrent,
                    "active_runs": len(self.active_tasks),
                    "runner_definitions": [d.name for d in self.runner_config.definitions],
                },
                headers=self._auth_headers(),
            )

    async def _poll_loop(self) -> None:
        """Poll for queued runs every 5 seconds."""
        while not _shutdown.is_set():
            try:
                if len(self.active_tasks) < self._max_concurrent:
                    await self._poll_for_run()
            except Exception as e:
                logger.error("Poll failed", error=str(e))

            # Clean up completed tasks
            completed = [rid for rid, task in self.active_tasks.items() if task.done()]
            for rid in completed:
                task = self.active_tasks.pop(rid)
                if task.exception():
                    logger.error(
                        "Run task failed with exception",
                        run_id=rid,
                        error=str(task.exception()),
                    )

            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=self._poll_interval)
                return
            except TimeoutError:
                pass

    async def _poll_for_run(self) -> None:
        """Try to claim the next queued run via the API."""
        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
            response = await client.get(
                f"/api/v2/listeners/listener-{self.identity.listener_id}/runs/next",
                headers=self._auth_headers(),
            )

            if response.status_code == 204:
                return  # No runs available
            response.raise_for_status()

            data = response.json()["data"]
            run_id = data["id"].removeprefix("run-")
            attrs = data.get("attributes", {})
            urls = attrs.get("presigned-urls", {})

            # Rewrite URLs for internal access
            urls = self._rewrite_urls(urls)

            # Extract resolved workspace variables from API response
            env_vars = [{"key": v["key"], "value": v["value"]} for v in attrs.get("env-vars", [])]
            terraform_vars = [
                {"key": v["key"], "value": v["value"]} for v in attrs.get("terraform-vars", [])
            ]

            task = asyncio.create_task(
                self._execute_run(
                    run_id=run_id,
                    phase="plan",
                    resource_cpu=attrs.get("resource-cpu", "1"),
                    resource_memory=attrs.get("resource-memory", "2Gi"),
                    presigned_urls=urls,
                    env_vars=env_vars,
                    terraform_vars=terraform_vars,
                    terraform_version=attrs.get("terraform-version", ""),
                    execution_backend=attrs.get("execution-backend", "tofu"),
                    service_account_name=attrs.get("service-account-name", ""),
                    plan_only=attrs.get("plan-only", False),
                )
            )
            self.active_tasks[run_id] = task

        logger.info("Claimed and started run", run_id=run_id)

    def _rewrite_urls(self, urls: dict[str, str]) -> dict[str, str]:
        """Rewrite presigned URL hostnames to the runner server_url.

        Storage backends generate URLs with the external base URL (e.g.
        https://terrapod.local) but runner Jobs need to reach the API via
        the internal K8s service URL (e.g. http://terrapod-api:8000).
        """
        server_url = self.runner_config.server_url or os.environ.get("TERRAPOD_API_URL", "")
        if not server_url:
            return urls

        target = urlparse(server_url)
        rewritten = {}
        for key, url in urls.items():
            parsed = urlparse(url)
            rewritten[key] = urlunparse(
                parsed._replace(
                    scheme=target.scheme,
                    netloc=target.netloc,
                )
            )
        return rewritten

    async def _execute_run(
        self,
        run_id: str,
        phase: str,
        resource_cpu: str,
        resource_memory: str,
        presigned_urls: dict,
        env_vars: list,
        terraform_vars: list,
        terraform_version: str = "",
        execution_backend: str = "",
        service_account_name: str = "",
        plan_only: bool = False,
    ) -> None:
        """Execute a run by creating a K8s Job and watching it (plan + apply)."""
        from terrapod.runner.job_template import build_job_spec

        # ── Pre-plan task stage ─────────────────────────────────────
        if not await self._check_task_stage(run_id, "pre_plan"):
            return

        # ── Plan phase ──────────────────────────────────────────────
        plan_spec = build_job_spec(
            run_id=run_id,
            phase="plan",
            runner_config=self.runner_config,
            presigned_urls=presigned_urls,
            env_vars=env_vars,
            terraform_vars=terraform_vars,
            resource_cpu=resource_cpu,
            resource_memory=resource_memory,
            terraform_version=terraform_version,
            execution_backend=execution_backend,
            service_account_name=service_account_name,
            plan_only=plan_only,
        )

        job_name, result = await self._create_and_watch_job(plan_spec, run_id, "plan")

        if result != "succeeded":
            logger.error("Plan failed", run_id=run_id, result=result)
            await self._report_status(run_id, "errored", f"Plan {result}")
            return

        # Parse has_changes from pod logs (entrypoint emits marker)
        has_changes = await self._parse_has_changes(job_name)

        # ── Post-plan task stage ────────────────────────────────────
        if not await self._check_task_stage(run_id, "post_plan"):
            return

        await self._report_status(run_id, "planned", has_changes=has_changes)

        # Plan-only runs stop here — no confirmation or apply phase
        if plan_only:
            logger.info("Plan-only run completed", run_id=run_id)
            return

        # ── Wait for confirmation ───────────────────────────────────
        confirmed = await self._wait_for_confirmation(run_id, timeout=3600)
        if not confirmed:
            return  # Run was discarded or canceled

        # ── Pre-apply task stage ────────────────────────────────────
        if not await self._check_task_stage(run_id, "pre_apply"):
            return

        # ── Apply phase ─────────────────────────────────────────────
        await self._report_status(run_id, "applying")

        apply_urls = await self._get_apply_urls(run_id)

        apply_spec = build_job_spec(
            run_id=run_id,
            phase="apply",
            runner_config=self.runner_config,
            presigned_urls=apply_urls,
            env_vars=env_vars,
            terraform_vars=terraform_vars,
            resource_cpu=resource_cpu,
            resource_memory=resource_memory,
            terraform_version=terraform_version,
            execution_backend=execution_backend,
            service_account_name=service_account_name,
        )

        apply_job_name, apply_result = await self._create_and_watch_job(apply_spec, run_id, "apply")

        if apply_result == "succeeded":
            logger.info("Apply succeeded", run_id=run_id)
            await self._report_status(run_id, "applied")
        else:
            logger.error("Apply failed", run_id=run_id, result=apply_result)
            await self._report_status(run_id, "errored", f"Apply {apply_result}")

    async def _create_and_watch_job(
        self,
        spec: dict,
        run_id: str,
        phase: str,
        max_retries: int = 2,
    ) -> tuple[str, str]:
        """Create a K8s Job and watch it, retrying on transient failures.

        Retries on: Job deleted externally, pod stuck in image pull errors,
        etc. Does NOT retry on: container ran and exited non-zero (failed),
        or timeout.

        Returns (job_name, result).
        """
        from terrapod.runner.job_manager import create_job, delete_job, get_job_status, watch_job

        job_name = spec.get("metadata", {}).get("name", "unknown")
        result = "timeout"

        for attempt in range(max_retries + 1):
            if attempt > 0:
                # Clean up previous Job before retry
                try:
                    await delete_job(job_name)
                except Exception:
                    pass
                for _ in range(15):
                    if await get_job_status(job_name) is None:
                        break
                    await asyncio.sleep(2)
                await asyncio.sleep(5 * attempt)

            try:
                job_name = await create_job(spec)
            except Exception as e:
                logger.error(
                    "Failed to create Job",
                    run_id=run_id,
                    phase=phase,
                    attempt=attempt + 1,
                    error=str(e),
                )
                result = "create_error"
                continue

            result = await watch_job(job_name, timeout_seconds=60 * 60)

            if result in ("succeeded", "failed", "timeout"):
                return job_name, result

            # Retryable: deleted, pod_error:*
            if attempt < max_retries:
                logger.warning(
                    "Job failed with retryable error, will retry",
                    run_id=run_id,
                    phase=phase,
                    result=result,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                )

        return job_name, result

    async def _report_status(
        self,
        run_id: str,
        status: str,
        error_message: str = "",
        has_changes: bool | None = None,
    ) -> None:
        """Report run status back to the API."""
        body: dict = {
            "status": status,
            "error_message": error_message,
        }
        if has_changes is not None:
            body["has_changes"] = has_changes

        logger.info("Reporting run status", run_id=run_id, status=status)

        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
            response = await client.patch(
                f"/api/v2/listeners/listener-{self.identity.listener_id}/runs/run-{run_id}",
                json=body,
                headers=self._auth_headers(),
            )
            if response.status_code >= 400:
                logger.warning(
                    "Failed to report status",
                    run_id=run_id,
                    status=status,
                    http_status=response.status_code,
                )

    async def _parse_has_changes(self, job_name: str) -> bool | None:
        """Parse PLAN_HAS_CHANGES marker from pod logs after plan completes.

        The runner entrypoint emits [entrypoint] PLAN_HAS_CHANGES=true/false
        to stdout after the plan finishes. We read the tail of the pod logs
        to find this marker.
        """
        try:
            from terrapod.runner.job_manager import get_pod_logs

            logs = await get_pod_logs(job_name, tail_lines=30)
            for line in logs.splitlines():
                if "PLAN_HAS_CHANGES=true" in line:
                    return True
                if "PLAN_HAS_CHANGES=false" in line:
                    return False
        except Exception as e:
            logger.warning("Failed to parse has_changes from pod logs", error=str(e))
        return None

    async def _wait_for_confirmation(self, run_id: str, timeout: int = 3600) -> bool:
        """Wait for a run to reach 'confirmed' status.

        Returns True if confirmed, False if discarded/canceled/errored or timeout.
        """
        deadline = time.monotonic() + timeout
        terminal = {"discarded", "canceled", "errored"}

        while time.monotonic() < deadline:
            if _shutdown.is_set():
                return False

            current_status = await self._get_run_status(run_id)
            if current_status == "confirmed":
                return True
            if current_status in terminal:
                logger.info(
                    "Run reached terminal state while waiting for confirmation",
                    run_id=run_id,
                    status=current_status,
                )
                return False

            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=5)
                return False  # Shutdown signaled
            except TimeoutError:
                pass

        logger.warning("Timed out waiting for run confirmation", run_id=run_id)
        return False

    async def _get_run_status(self, run_id: str) -> str:
        """Get current run status via API.

        Returns "unknown" on transient errors so callers don't mistake
        a failed API call for the run actually being in errored state.
        """
        try:
            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=10) as client:
                response = await client.get(
                    f"/api/v2/runs/run-{run_id}",
                    headers=self._auth_headers(),
                )
                if response.status_code != 200:
                    logger.warning(
                        "Failed to fetch run status", run_id=run_id, status=response.status_code
                    )
                    return "unknown"
                return response.json()["data"]["attributes"]["status"]
        except Exception as e:
            logger.warning("Error fetching run status", run_id=run_id, error=str(e))
            return "unknown"

    async def _get_apply_urls(self, run_id: str) -> dict[str, str]:
        """Get presigned URLs for the apply phase."""
        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
            response = await client.get(
                f"/api/v2/listeners/listener-{self.identity.listener_id}"
                f"/runs/run-{run_id}/apply-urls",
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            return self._rewrite_urls(response.json())

    async def _check_task_stage(self, run_id: str, stage_name: str) -> bool:
        """Check for applicable run tasks at a stage boundary.

        Creates a task stage if there are enabled run tasks for this stage,
        then polls until all results are resolved. Returns True if the run
        should proceed, False if blocked (mandatory failure, errored, or canceled).
        """
        # Create task stage via API
        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
            response = await client.post(
                f"/api/v2/runs/run-{run_id}/task-stages",
                json={"stage": stage_name},
                headers=self._auth_headers(),
            )
            if response.status_code == 204:
                return True  # No applicable tasks
            if response.status_code != 201:
                logger.warning("Failed to create task stage", status=response.status_code)
                return True  # Don't block on API errors

            ts_data = response.json()["data"]
            ts_id = ts_data["id"]

        # Poll until resolved
        while not _shutdown.is_set():
            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=10) as client:
                response = await client.get(
                    f"/api/v2/task-stages/{ts_id}",
                    headers=self._auth_headers(),
                )
                if response.status_code != 200:
                    return False

                status = response.json()["data"]["attributes"]["status"]
                if status in ("passed", "overridden"):
                    return True
                elif status in ("failed", "errored", "canceled"):
                    return False

            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=5)
                return False
            except TimeoutError:
                pass

        return False


def _handle_signals() -> None:
    """Register signal handlers for graceful shutdown."""
    loop = asyncio.get_event_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _shutdown.set())


def main() -> None:
    """Main entry point for the listener."""
    configure_logging(json_logs=True, log_level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.info("Starting Terrapod runner listener")

    listener = RunnerListener()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    _handle_signals()

    try:
        loop.run_until_complete(listener.start())
    except KeyboardInterrupt:
        _shutdown.set()
    finally:
        loop.close()
        logger.info("Listener stopped")


if __name__ == "__main__":
    main()
