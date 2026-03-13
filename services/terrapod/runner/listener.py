"""Runner listener main loop.

Entrypoint: python -m terrapod.runner.listener

The listener:
1. Establishes identity (join pool via token exchange)
2. Starts heartbeat loop (every 60s)
3. Polls for queued runs (every 5s)
4. Spawns K8s Jobs for claimed runs
5. Watches Jobs to completion and reports status back
6. Serves /health and /ready HTTP endpoints for K8s probes
"""

import asyncio
import base64
import json
import os
import signal
import time

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
        self._health_port = int(os.environ.get("TERRAPOD_HEALTH_PORT", "8081"))
        self._identity_ready = False
        self._last_heartbeat_at: float | None = None

        # Prometheus metrics — separate registry to avoid colliding with API metrics
        from prometheus_client import CollectorRegistry, Gauge

        self._metrics_registry = CollectorRegistry()
        self._metric_active_runs = Gauge(
            "terrapod_listener_active_runs",
            "Number of currently active runs",
            registry=self._metrics_registry,
        )
        self._metric_identity_ready = Gauge(
            "terrapod_listener_identity_ready",
            "Whether listener identity is established (1=yes, 0=no)",
            registry=self._metrics_registry,
        )
        self._metric_heartbeat_age = Gauge(
            "terrapod_listener_heartbeat_age_seconds",
            "Seconds since last successful heartbeat",
            registry=self._metrics_registry,
        )

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
        self._identity_ready = True
        logger.info(
            "Listener started",
            listener_id=str(self.identity.listener_id),
            name=self.identity.name,
            pod_name=os.environ.get("POD_NAME", ""),
        )

        # Start concurrent loops
        await asyncio.gather(
            self._health_server(),
            self._heartbeat_loop(),
            self._poll_loop(),
            self._shutdown_waiter(),
        )

    # ── Health server ────────────────────────────────────────────────

    async def _health_server(self) -> None:
        """Lightweight HTTP health server for K8s probes (stdlib only)."""
        server = await asyncio.start_server(
            self._handle_health_request,
            "0.0.0.0",
            self._health_port,
        )
        logger.info("Health server listening", port=self._health_port)

        try:
            await _shutdown.wait()
        finally:
            server.close()
            await server.wait_closed()

    async def _handle_health_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single HTTP request on the health port."""
        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=5)
            request_line = data.decode("utf-8", errors="replace").split("\r\n")[0]
            parts = request_line.split(" ")
            path = parts[1] if len(parts) > 1 else "/"

            if path == "/health":
                body = json.dumps({"status": "ok"})
                status = "200 OK"
                content_type = "application/json"
            elif path == "/ready":
                ready, reason = self._check_readiness()
                if ready:
                    body = json.dumps({"status": "ready"})
                    status = "200 OK"
                else:
                    body = json.dumps({"status": "not_ready", "reason": reason})
                    status = "503 Service Unavailable"
                content_type = "application/json"
            elif path == "/metrics":
                body, content_type = self._generate_metrics()
                status = "200 OK"
            else:
                body = json.dumps({"error": "not found"})
                status = "404 Not Found"
                content_type = "application/json"

            response = (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )
            writer.write(response.encode())
            await writer.drain()
        except Exception:
            pass  # Don't crash on malformed probe requests
        finally:
            writer.close()

    def _check_readiness(self) -> tuple[bool, str]:
        """Check if the listener is ready to accept work.

        Ready when: identity established + last heartbeat within 3x interval.
        """
        if not self._identity_ready:
            return False, "identity not established"

        if self._last_heartbeat_at is None:
            return False, "no heartbeat sent yet"

        heartbeat_age = time.monotonic() - self._last_heartbeat_at
        max_age = self._heartbeat_interval * 3
        if heartbeat_age > max_age:
            return False, f"heartbeat stale ({heartbeat_age:.0f}s > {max_age}s)"

        return True, ""

    def _generate_metrics(self) -> tuple[str, str]:
        """Generate Prometheus metrics for the listener."""
        from prometheus_client import generate_latest

        self._metric_active_runs.set(len(self.active_tasks))
        self._metric_identity_ready.set(1 if self._identity_ready else 0)

        if self._last_heartbeat_at is not None:
            self._metric_heartbeat_age.set(time.monotonic() - self._last_heartbeat_at)
        else:
            self._metric_heartbeat_age.set(-1)

        body = generate_latest(self._metrics_registry).decode("utf-8")
        return body, "text/plain; version=0.0.4; charset=utf-8"

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
        self._last_heartbeat_at = time.monotonic()

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

            # Request a runner token for this run
            runner_token = await self._get_runner_token(run_id)

            # Extract resolved workspace variables from API response
            env_vars = [{"key": v["key"], "value": v["value"]} for v in attrs.get("env-vars", [])]
            terraform_vars = [
                {"key": v["key"], "value": v["value"]} for v in attrs.get("terraform-vars", [])
            ]

            phase = attrs.get("phase", "plan")
            common_kwargs = dict(
                run_id=run_id,
                resource_cpu=attrs.get("resource-cpu", "1"),
                resource_memory=attrs.get("resource-memory", "2Gi"),
                runner_token=runner_token,
                env_vars=env_vars,
                terraform_vars=terraform_vars,
                terraform_version=attrs.get("terraform-version", ""),
                execution_backend=attrs.get("execution-backend", "tofu"),
                var_files=attrs.get("var-files", []),
            )

            if phase == "apply":
                coro = self._execute_apply(**common_kwargs)
            else:
                coro = self._execute_plan(
                    **common_kwargs,
                    plan_only=attrs.get("plan-only", False),
                )

            task = asyncio.create_task(coro)
            self.active_tasks[run_id] = task

        logger.info("Claimed and started run", run_id=run_id, phase=phase)

    async def _get_runner_token(self, run_id: str) -> str:
        """Request a short-lived runner token from the API."""
        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
            response = await client.post(
                f"/api/v2/listeners/listener-{self.identity.listener_id}"
                f"/runs/run-{run_id}/runner-token",
                json={},
                headers=self._auth_headers(),
            )
            response.raise_for_status()
            return response.json()["token"]

    async def _create_auth_secret(
        self, run_id: str, token: str, job_name: str, job_uid: str
    ) -> str:
        """Create a K8s Secret containing the runner token with ownerReference to the Job.

        Returns the Secret name.
        """
        from kubernetes import client as k8s_client

        from terrapod.runner.job_manager import _get_core_api

        namespace = os.environ.get("TERRAPOD_RUNNER_NAMESPACE", "terrapod-runners")
        run_short = run_id[:8]
        secret_name = f"tprun-{run_short}-auth"

        secret = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(
                name=secret_name,
                namespace=namespace,
                labels={
                    "app.kubernetes.io/name": "terrapod-runner",
                    "terrapod.io/run-id": run_id,
                },
                owner_references=[
                    k8s_client.V1OwnerReference(
                        api_version="batch/v1",
                        kind="Job",
                        name=job_name,
                        uid=job_uid,
                        block_owner_deletion=True,
                    )
                ],
            ),
            type="Opaque",
            string_data={"token": token},
        )

        core_api = _get_core_api()
        core_api.create_namespaced_secret(namespace=namespace, body=secret)
        logger.info("Created auth secret", secret=secret_name, job=job_name)
        return secret_name

    async def _execute_plan(
        self,
        run_id: str,
        resource_cpu: str,
        resource_memory: str,
        runner_token: str,
        env_vars: list,
        terraform_vars: list,
        terraform_version: str = "",
        execution_backend: str = "",
        plan_only: bool = False,
        var_files: list[str] | None = None,
    ) -> None:
        """Execute the plan phase of a run.

        After plan completes, reports 'planned' status. The API handles
        auto-confirm → 'confirmed'. A subsequent poll cycle (this or another
        listener) picks up the confirmed run for the apply phase.
        """
        from terrapod.runner.job_template import build_job_spec

        run_short = run_id[:8]
        auth_secret_name = f"tprun-{run_short}-auth"

        # ── Pre-plan task stage ─────────────────────────────────────
        if not await self._check_task_stage(run_id, "pre_plan"):
            return

        # ── Plan phase ──────────────────────────────────────────────
        plan_spec = build_job_spec(
            run_id=run_id,
            phase="plan",
            runner_config=self.runner_config,
            auth_secret_name=auth_secret_name,
            env_vars=env_vars,
            terraform_vars=terraform_vars,
            resource_cpu=resource_cpu,
            resource_memory=resource_memory,
            terraform_version=terraform_version,
            execution_backend=execution_backend,
            plan_only=plan_only,
            var_files=var_files,
        )

        job_name, result = await self._create_and_watch_job(
            plan_spec, run_id, "plan", runner_token=runner_token
        )

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
        logger.info("Plan phase completed", run_id=run_id, plan_only=plan_only)

    async def _execute_apply(
        self,
        run_id: str,
        resource_cpu: str,
        resource_memory: str,
        runner_token: str,
        env_vars: list,
        terraform_vars: list,
        terraform_version: str = "",
        execution_backend: str = "",
        var_files: list[str] | None = None,
    ) -> None:
        """Execute the apply phase of a confirmed run.

        Claimed independently from plan — any listener can pick up a
        confirmed run, making the system resilient to listener restarts.
        """
        from terrapod.runner.job_template import build_job_spec

        run_short = run_id[:8]
        auth_secret_name = f"tprun-{run_short}-auth"

        # ── Pre-apply task stage ────────────────────────────────────
        if not await self._check_task_stage(run_id, "pre_apply"):
            return

        # ── Apply phase ─────────────────────────────────────────────
        apply_spec = build_job_spec(
            run_id=run_id,
            phase="apply",
            runner_config=self.runner_config,
            auth_secret_name=auth_secret_name,
            env_vars=env_vars,
            terraform_vars=terraform_vars,
            resource_cpu=resource_cpu,
            resource_memory=resource_memory,
            terraform_version=terraform_version,
            execution_backend=execution_backend,
            var_files=var_files,
        )

        apply_job_name, apply_result = await self._create_and_watch_job(
            apply_spec, run_id, "apply", runner_token=runner_token
        )

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
        runner_token: str = "",
    ) -> tuple[str, str]:
        """Create a K8s Job, create auth Secret with ownerRef, and watch.

        Retries on: Job deleted externally, pod stuck in image pull errors,
        etc. Does NOT retry on: container ran and exited non-zero (failed),
        or timeout.

        Returns (job_name, result).
        """
        from terrapod.runner.job_manager import (
            create_job,
            delete_job,
            get_job_status,
            get_job_uid,
            watch_job,
        )

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

            # Create auth Secret with ownerReference to the Job
            if runner_token:
                try:
                    job_uid = await get_job_uid(job_name)
                    await self._create_auth_secret(run_id, runner_token, job_name, job_uid)
                except Exception as e:
                    logger.error(
                        "Failed to create auth secret",
                        run_id=run_id,
                        phase=phase,
                        error=str(e),
                    )
                    result = "secret_error"
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
