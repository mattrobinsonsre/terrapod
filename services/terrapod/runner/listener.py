"""Runner listener — stateless fire-and-forget Job launcher with SSE + polling.

Entrypoint: python -m terrapod.runner.listener

The listener:
1. Establishes identity (join pool via token exchange)
2. Starts heartbeat loop (every 60s)
3. Connects to the API via SSE for event-driven work
4. Runs a polling fallback loop (every 30s) alongside SSE
5. On run_available: claims run, launches K8s Job, reports Job name — DONE
6. On check_job_status: queries K8s, POSTs result back
7. On stream_logs: reads pod logs, PUTs log bytes back
8. On cancel_job: deletes K8s Job
9. Serves /health and /ready HTTP endpoints for K8s probes

SSE provides sub-second responsiveness for run claiming. The polling
fallback ensures bounded worst-case latency (~30s) even if SSE is
disrupted by proxies, CDNs, or network interruptions.

The listener holds ZERO run state. If it dies, another listener in the
pool can answer Job status queries. The API reconciler owns all run
lifecycle state transitions.
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
    """Stateless Job launcher — ARC-pattern controller."""

    def __init__(self):
        self.identity = None
        self.runner_config = load_runner_config()
        self._heartbeat_interval = int(os.environ.get("TERRAPOD_HEARTBEAT_INTERVAL", "60"))
        self._max_concurrent = int(os.environ.get("TERRAPOD_MAX_CONCURRENT", "3"))
        self._health_port = int(os.environ.get("TERRAPOD_HEALTH_PORT", "8081"))
        self._sse_retry_interval = int(os.environ.get("TERRAPOD_SSE_RETRY_INTERVAL", "5"))
        self._poll_interval = int(os.environ.get("TERRAPOD_POLL_INTERVAL", "30"))
        self._identity_ready = False
        self._last_heartbeat_at: float | None = None
        self._active_launches = 0  # count of concurrent launch operations

        # Prometheus metrics — separate registry to avoid colliding with API metrics
        from prometheus_client import CollectorRegistry, Gauge

        self._metrics_registry = CollectorRegistry()
        self._metric_active_runs = Gauge(
            "terrapod_listener_active_runs",
            "Number of currently active launches",
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

        # Start concurrent loops — SSE for sub-second responsiveness,
        # poll loop as reliability fallback (~30s worst-case latency)
        await asyncio.gather(
            self._health_server(),
            self._heartbeat_loop(),
            self._sse_loop(),
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
                http_status = "200 OK"
                content_type = "application/json"
            elif path == "/ready":
                ready, reason = self._check_readiness()
                if ready:
                    body = json.dumps({"status": "ready"})
                    http_status = "200 OK"
                else:
                    body = json.dumps({"status": "not_ready", "reason": reason})
                    http_status = "503 Service Unavailable"
                content_type = "application/json"
            elif path == "/metrics":
                body, content_type = self._generate_metrics()
                http_status = "200 OK"
            else:
                body = json.dumps({"error": "not found"})
                http_status = "404 Not Found"
                content_type = "application/json"

            response = (
                f"HTTP/1.1 {http_status}\r\n"
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
        """Check if the listener is ready to accept work."""
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

        self._metric_active_runs.set(self._active_launches)
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
        logger.info("Shutdown signal received")

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
                    "active_runs": self._active_launches,
                    "runner_definitions": [d.name for d in self.runner_config.definitions],
                },
                headers=self._auth_headers(),
            )
        self._last_heartbeat_at = time.monotonic()

    # ── SSE Event Loop ───────────────────────────────────────────────

    async def _sse_loop(self) -> None:
        """Connect to the API SSE endpoint and handle events.

        Reconnects automatically on disconnect with exponential backoff.
        """
        while not _shutdown.is_set():
            try:
                await self._sse_connect()
            except Exception as e:
                logger.error("SSE connection failed", error=str(e))

            if _shutdown.is_set():
                return

            logger.info("SSE reconnecting", delay=self._sse_retry_interval)
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=self._sse_retry_interval)
                return
            except TimeoutError:
                pass

    async def _sse_connect(self) -> None:
        """Maintain a single SSE connection and dispatch events."""
        url = (
            f"{self.identity.api_url}/api/v2/listeners/listener-{self.identity.listener_id}/events"
        )
        logger.info("SSE connecting", url=url)

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, headers=self._auth_headers()) as response:
                response.raise_for_status()
                logger.info("SSE connected")

                event_type = ""
                event_data = ""

                async for line in response.aiter_lines():
                    if _shutdown.is_set():
                        return

                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        event_data = line[5:].strip()
                    elif line == "":
                        # Empty line = end of event
                        if event_type and event_data:
                            asyncio.create_task(self._dispatch_event(event_type, event_data))
                        event_type = ""
                        event_data = ""
                    # Ignore comment lines (keepalives start with ":")

    # ── Poll Fallback Loop ─────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll for claimable runs as a fallback alongside SSE.

        SSE delivers run_available events with sub-second latency, but any
        layer in the proxy chain (Cloudflare, CDN, ingress, BFF) can silently
        drop or buffer the connection. This loop ensures bounded worst-case
        latency (~30s) by periodically checking for available work regardless
        of SSE state.
        """
        while not _shutdown.is_set():
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=self._poll_interval)
                return  # Shutdown signaled
            except TimeoutError:
                pass

            try:
                await self._handle_run_available()
            except Exception as e:
                logger.warning("Poll fallback failed", error=str(e))

    # ── SSE Event Dispatch ────────────────────────────────────────

    async def _dispatch_event(self, event_type: str, raw_data: str) -> None:
        """Dispatch an SSE event to the appropriate handler."""
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            logger.warning("Invalid SSE event data", event_type=event_type)
            return

        if event_type == "run_available":
            await self._handle_run_available()
        elif event_type == "check_job_status":
            await self._handle_check_job_status(data)
        elif event_type == "stream_logs":
            await self._handle_stream_logs(data)
        elif event_type == "cancel_job":
            await self._handle_cancel_job(data)
        else:
            logger.debug("Unknown SSE event", event_type=event_type)

    # ── Event Handlers ───────────────────────────────────────────────

    async def _handle_run_available(self) -> None:
        """Claim a run, launch a Job, report back — done."""
        if self._active_launches >= self._max_concurrent:
            return

        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
            response = await client.get(
                f"/api/v2/listeners/listener-{self.identity.listener_id}/runs/next",
                headers=self._auth_headers(),
            )

            if response.status_code == 204:
                return  # No runs available (another listener claimed it)
            response.raise_for_status()

            data = response.json()["data"]
            run_id = data["id"].removeprefix("run-")
            attrs = data.get("attributes", {})

        # Launch the Job in the background (fire-and-forget)
        self._active_launches += 1
        try:
            await self._launch_run(run_id, attrs)
        finally:
            self._active_launches -= 1

    async def _launch_run(self, run_id: str, attrs: dict) -> None:
        """Build and launch a K8s Job for a claimed run, then report back."""
        from terrapod.runner.job_manager import create_job, get_job_uid
        from terrapod.runner.job_template import build_job_spec

        phase = attrs.get("phase", "plan")
        runner_token = await self._get_runner_token(run_id)

        env_vars = [{"key": v["key"], "value": v["value"]} for v in attrs.get("env-vars", [])]
        terraform_vars = [
            {"key": v["key"], "value": v["value"]} for v in attrs.get("terraform-vars", [])
        ]

        run_short = run_id[:16]
        auth_secret_name = f"tprun-{run_short}-auth"

        spec = build_job_spec(
            run_id=run_id,
            phase=phase,
            runner_config=self.runner_config,
            auth_secret_name=auth_secret_name,
            env_vars=env_vars,
            terraform_vars=terraform_vars,
            resource_cpu=attrs.get("resource-cpu", "1"),
            resource_memory=attrs.get("resource-memory", "2Gi"),
            terraform_version=attrs.get("terraform-version", ""),
            execution_backend=attrs.get("execution-backend", "tofu"),
            plan_only=attrs.get("plan-only", False),
            var_files=attrs.get("var-files", []),
            target_addrs=attrs.get("target-addrs"),
            replace_addrs=attrs.get("replace-addrs"),
            refresh_only=attrs.get("refresh-only", False),
            refresh=attrs.get("refresh", True),
            allow_empty_apply=attrs.get("allow-empty-apply", False),
        )

        namespace = os.environ.get("TERRAPOD_RUNNER_NAMESPACE", "terrapod-runners")

        try:
            job_name = await create_job(spec)
        except Exception as e:
            logger.error("Failed to create Job", run_id=run_id, error=str(e))
            return

        # Create auth Secret with ownerReference to the Job
        try:
            job_uid = await get_job_uid(job_name)
            await self._create_auth_secret(run_id, runner_token, job_name, job_uid)
        except Exception as e:
            logger.error("Failed to create auth secret", run_id=run_id, error=str(e))

        # Report Job launched to the API
        try:
            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
                await client.post(
                    f"/api/v2/listeners/listener-{self.identity.listener_id}"
                    f"/runs/run-{run_id}/job-launched",
                    json={"job_name": job_name, "job_namespace": namespace},
                    headers=self._auth_headers(),
                )
        except Exception as e:
            logger.error("Failed to report job-launched", run_id=run_id, error=str(e))

        logger.info(
            "Job launched",
            run_id=run_id,
            phase=phase,
            job=job_name,
        )

    async def _handle_check_job_status(self, data: dict) -> None:
        """Query K8s for Job status and POST the result back to the API."""
        from terrapod.runner.job_manager import get_job_status

        job_name = data.get("job_name", "")
        job_namespace = data.get("job_namespace", "")
        run_id = data.get("run_id", "")
        phase = data.get("phase", "plan")

        if not job_name or not run_id:
            return

        try:
            status = await get_job_status(job_name, namespace=job_namespace)
            if status is None:
                status = "deleted"
        except Exception as e:
            logger.warning("Failed to get Job status", job=job_name, error=str(e))
            return

        try:
            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=10) as client:
                await client.post(
                    f"/api/v2/listeners/listener-{self.identity.listener_id}"
                    f"/runs/run-{run_id}/job-status",
                    json={"status": status, "phase": phase},
                    headers=self._auth_headers(),
                )
        except Exception as e:
            logger.warning("Failed to report Job status", run_id=run_id, error=str(e))

    async def _handle_stream_logs(self, data: dict) -> None:
        """Read pod logs from K8s and PUT them back to the API."""
        from terrapod.runner.job_manager import get_pod_logs

        job_name = data.get("job_name", "")
        job_namespace = data.get("job_namespace", "")
        run_id = data.get("run_id", "")
        tail_lines = data.get("tail_lines", 500)
        phase = data.get("phase", "plan")

        if not job_name or not run_id:
            return

        try:
            logs = await get_pod_logs(job_name, namespace=job_namespace, tail_lines=tail_lines)
            if not logs:
                return
        except Exception:
            return  # Pod may not have logs yet

        try:
            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=10) as client:
                await client.put(
                    f"/api/v2/listeners/listener-{self.identity.listener_id}"
                    f"/runs/run-{run_id}/log-stream",
                    params={"phase": phase},
                    content=logs.encode() if isinstance(logs, str) else logs,
                    headers={
                        **self._auth_headers(),
                        "Content-Type": "application/octet-stream",
                    },
                )
        except Exception:
            pass  # Best-effort log streaming

    async def _handle_cancel_job(self, data: dict) -> None:
        """Delete a K8s Job for a canceled run."""
        from terrapod.runner.job_manager import delete_job

        job_name = data.get("job_name", "")
        job_namespace = data.get("job_namespace", "")

        if not job_name:
            return

        try:
            await delete_job(job_name, namespace=job_namespace)
            logger.info("Job deleted (canceled)", job=job_name)
        except Exception as e:
            logger.warning("Failed to delete Job", job=job_name, error=str(e))

    # ── Shared Helpers ───────────────────────────────────────────────

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
        """Create a K8s Secret containing the runner token with ownerReference."""
        from kubernetes import client as k8s_client

        from terrapod.runner.job_manager import _get_core_api

        namespace = os.environ.get("TERRAPOD_RUNNER_NAMESPACE", "terrapod-runners")
        run_short = run_id[:16]
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
