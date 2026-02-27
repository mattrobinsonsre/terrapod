"""Runner listener main loop.

Entrypoint: python -m terrapod.runner.listener

The listener:
1. Establishes identity (local auto-register or remote join)
2. Starts heartbeat loop (every 60s)
3. Polls for queued runs (every 5s)
4. Spawns K8s Jobs for claimed runs
5. Watches Jobs to completion and reports status back
"""

import asyncio
import json
import os
import signal
import time
import uuid
from urllib.parse import urlparse, urlunparse

from terrapod.config import load_runner_config
from terrapod.db.models import Workspace
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

    async def start(self) -> None:
        """Main entry point — initialize and start loops."""
        # Initialize infrastructure
        from terrapod.db.session import init_db
        from terrapod.redis.client import init_redis
        from terrapod.runner.identity import establish_identity
        from terrapod.runner.job_manager import init_k8s

        await init_db()
        await init_redis()
        init_k8s()

        # Establish identity
        self.identity = await establish_identity()
        logger.info(
            "Listener started",
            listener_id=str(self.identity.listener_id),
            name=self.identity.name,
            mode=self.identity.mode,
        )

        # Recovery: check for orphaned runs from a previous crash
        await self._recover_orphaned_runs()

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
        """Send heartbeat — local uses Redis directly, remote uses API."""
        if self.identity.is_local:
            from terrapod.redis.client import get_redis_client

            redis = get_redis_client()
            prefix = f"tp:listener:{self.identity.listener_id}"
            ttl = 180

            runner_defs = [d.name for d in self.runner_config.definitions]

            await redis.setex(f"{prefix}:status", ttl, "online")
            await redis.setex(f"{prefix}:heartbeat", ttl, str(int(time.time())))
            await redis.setex(f"{prefix}:capacity", ttl, str(self._max_concurrent))
            await redis.setex(f"{prefix}:active_runs", ttl, str(len(self.active_tasks)))
            await redis.setex(f"{prefix}:runner_defs", ttl, json.dumps(runner_defs))
        else:
            import base64

            import httpx

            headers = {}
            if self.identity.certificate_pem:
                cert_b64 = base64.b64encode(self.identity.certificate_pem.encode()).decode()
                headers["X-Terrapod-Client-Cert"] = cert_b64

            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=10) as client:
                await client.post(
                    f"/api/v2/listeners/listener-{self.identity.listener_id}/heartbeat",
                    json={
                        "capacity": self._max_concurrent,
                        "active_runs": len(self.active_tasks),
                        "runner_definitions": [d.name for d in self.runner_config.definitions],
                    },
                    headers=headers,
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
        """Try to claim the next queued run."""
        if self.identity.is_local:
            await self._poll_local()
        else:
            await self._poll_remote()

    @property
    def _api_base_url(self) -> str:
        """API base URL for internal HTTP calls (used by local listener)."""
        return self.runner_config.server_url or os.environ.get(
            "TERRAPOD_API_URL", "http://localhost:8000"
        )

    async def _fetch_urls_from_api(self, run_id: str, phase: str) -> dict[str, str]:
        """Fetch presigned URLs from the API via HTTP.

        The API process owns storage initialization (and HMAC secrets for
        filesystem backend).  The listener must not generate presigned URLs
        itself — it asks the API instead.
        """
        import httpx

        lid = f"listener-{self.identity.listener_id}"
        endpoint = f"/api/v2/listeners/{lid}/runs/run-{run_id}/{phase}-urls"

        async with httpx.AsyncClient(base_url=self._api_base_url, timeout=30) as client:
            response = await client.get(endpoint)
            response.raise_for_status()
            urls = response.json()

        return self._rewrite_urls(urls)

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

    async def _poll_local(self) -> None:
        """Poll using direct DB access (local listener)."""
        from terrapod.db.session import get_db_session
        from terrapod.services import agent_pool_service, run_service

        async with get_db_session() as db:
            listener = await agent_pool_service.get_listener(db, self.identity.listener_id)
            if listener is None:
                return

            run = await run_service.claim_next_run(db, listener)
            if run is None:
                return

            # Resolve variables (needs DB)
            resolved = await resolve_run_variables(db, run)

            run_id = str(run.id)
            resource_cpu = run.resource_cpu
            resource_memory = run.resource_memory
            terraform_version = run.terraform_version
            pool_id = run.pool_id

        # Get presigned URLs from the API (not locally — the API owns the
        # storage HMAC secret for filesystem backend)
        urls = await self._fetch_urls_from_api(run_id, "plan")

        # Get service account from pool
        sa_name = ""
        if pool_id:
            async with get_db_session() as db:
                pool = await agent_pool_service.get_pool(db, pool_id)
                if pool:
                    sa_name = pool.service_account_name

        # Start execution in background task
        task = asyncio.create_task(
            self._execute_run(
                run_id=run_id,
                phase="plan",
                resource_cpu=resource_cpu,
                resource_memory=resource_memory,
                presigned_urls=urls,
                env_vars=resolved.get("env", []),
                terraform_vars=resolved.get("terraform", []),
                terraform_version=terraform_version,
                service_account_name=sa_name,
            )
        )
        self.active_tasks[run_id] = task

        logger.info("Claimed and started run", run_id=run_id)

    async def _poll_remote(self) -> None:
        """Poll via API (remote listener)."""
        import base64

        import httpx

        headers = {}
        if self.identity.certificate_pem:
            cert_b64 = base64.b64encode(self.identity.certificate_pem.encode()).decode()
            headers["X-Terrapod-Client-Cert"] = cert_b64

        async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
            response = await client.get(
                f"/api/v2/listeners/listener-{self.identity.listener_id}/runs/next",
                headers=headers,
            )

            if response.status_code == 204:
                return  # No runs available
            response.raise_for_status()

            data = response.json()["data"]
            run_id = data["id"].removeprefix("run-")
            attrs = data.get("attributes", {})
            urls = attrs.get("presigned-urls", {})

            task = asyncio.create_task(
                self._execute_run(
                    run_id=run_id,
                    phase="plan",
                    resource_cpu=attrs.get("resource-cpu", "1"),
                    resource_memory=attrs.get("resource-memory", "2Gi"),
                    presigned_urls=urls,
                    env_vars=[],  # Remote gets vars from presigned URLs
                    terraform_vars=[],
                    terraform_version=attrs.get("terraform-version", ""),
                )
            )
            self.active_tasks[run_id] = task

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
        service_account_name: str = "",
    ) -> None:
        """Execute a run by creating a K8s Job and watching it (plan + apply)."""
        from terrapod.runner.job_manager import create_job, watch_job
        from terrapod.runner.job_template import build_job_spec

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
            service_account_name=service_account_name,
        )

        job_name = await create_job(plan_spec)
        result = await watch_job(job_name, timeout_seconds=60 * 60)

        if result == "succeeded":
            await self._report_status(run_id, "planned")
        else:
            await self._report_status(run_id, "errored", f"Plan {result}")
            return

        # ── Wait for confirmation ───────────────────────────────────
        confirmed = await self._wait_for_confirmation(run_id, timeout=3600)
        if not confirmed:
            return  # Run was discarded or canceled

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
            service_account_name=service_account_name,
        )

        apply_job_name = await create_job(apply_spec)
        apply_result = await watch_job(apply_job_name, timeout_seconds=60 * 60)

        if apply_result == "succeeded":
            await self._report_status(run_id, "applied")
        else:
            await self._report_status(run_id, "errored", f"Apply {apply_result}")

    async def _report_status(self, run_id: str, status: str, error_message: str = "") -> None:
        """Report run status back to the API."""
        if self.identity.is_local:
            from terrapod.db.session import get_db_session
            from terrapod.services import run_service

            async with get_db_session() as db:
                run = await run_service.get_run(db, uuid.UUID(run_id))
                if run:
                    await run_service.transition_run(db, run, status, error_message=error_message)

                    # Auto-apply if configured
                    if status == "planned" and run.auto_apply and not run.plan_only:
                        await run_service.transition_run(db, run, "confirmed")

                    # Unlock workspace on terminal state
                    if status in run_service.TERMINAL_STATES:
                        ws = await db.get(Workspace, run.workspace_id)
                        if ws and ws.locked:
                            ws.locked = False
                            ws.lock_id = None
        else:
            import base64

            import httpx

            headers = {}
            if self.identity.certificate_pem:
                cert_b64 = base64.b64encode(self.identity.certificate_pem.encode()).decode()
                headers["X-Terrapod-Client-Cert"] = cert_b64

            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
                await client.patch(
                    f"/api/v2/listeners/listener-{self.identity.listener_id}/runs/run-{run_id}",
                    json={
                        "status": status,
                        "error_message": error_message,
                    },
                    headers=headers,
                )

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
        """Get current run status."""
        if self.identity.is_local:
            from terrapod.db.session import get_db_session
            from terrapod.services import run_service

            async with get_db_session() as db:
                run = await run_service.get_run(db, uuid.UUID(run_id))
                return run.status if run else "errored"
        else:
            import base64

            import httpx

            headers = {}
            if self.identity.certificate_pem:
                cert_b64 = base64.b64encode(self.identity.certificate_pem.encode()).decode()
                headers["X-Terrapod-Client-Cert"] = cert_b64

            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=10) as client:
                response = await client.get(
                    f"/api/v2/runs/run-{run_id}",
                    headers=headers,
                )
                if response.status_code != 200:
                    return "errored"
                return response.json()["data"]["attributes"]["status"]

    async def _get_apply_urls(self, run_id: str) -> dict[str, str]:
        """Get presigned URLs for the apply phase."""
        if self.identity.is_local:
            return await self._fetch_urls_from_api(run_id, "apply")
        else:
            import base64

            import httpx

            headers = {}
            if self.identity.certificate_pem:
                cert_b64 = base64.b64encode(self.identity.certificate_pem.encode()).decode()
                headers["X-Terrapod-Client-Cert"] = cert_b64

            async with httpx.AsyncClient(base_url=self.identity.api_url, timeout=30) as client:
                response = await client.get(
                    f"/api/v2/listeners/listener-{self.identity.listener_id}"
                    f"/runs/run-{run_id}/apply-urls",
                    headers=headers,
                )
                response.raise_for_status()
                return response.json()

    async def _recover_orphaned_runs(self) -> None:
        """Check for runs that were active when we crashed and recover them."""
        if not self.identity.is_local:
            return  # Remote listeners can't do direct DB recovery

        from terrapod.db.session import get_db_session
        from terrapod.runner.job_manager import get_job_status
        from terrapod.services import run_service

        async with get_db_session() as db:
            orphaned = await run_service.find_orphaned_runs(db, [self.identity.listener_id])

            for run in orphaned:
                run_id = str(run.id)
                run_short = run_id[:8]

                # Determine which phase was active
                phase = "apply" if run.apply_started_at else "plan"
                job_name = f"tprun-{run_short}-{phase}"

                job_status = await get_job_status(job_name)

                if job_status == "running":
                    # Resume watching
                    logger.info("Resuming watch for orphaned Job", job=job_name, run_id=run_id)
                    task = asyncio.create_task(self._resume_watch(run_id, job_name))
                    self.active_tasks[run_id] = task
                elif job_status in ("succeeded", "failed"):
                    target = (
                        "planned"
                        if job_status == "succeeded" and phase == "plan"
                        else ("applied" if job_status == "succeeded" else "errored")
                    )
                    await run_service.transition_run(
                        db,
                        run,
                        target,
                        error_message=f"Recovered from crash: {job_status}"
                        if target == "errored"
                        else "",
                    )
                    logger.info("Recovered orphaned run", run_id=run_id, status=target)
                else:
                    # Job gone — mark errored
                    await run_service.transition_run(
                        db,
                        run,
                        "errored",
                        error_message="Listener crashed and Job not found",
                    )
                    logger.warning("Orphaned run marked errored", run_id=run_id)

    async def _resume_watch(self, run_id: str, job_name: str) -> None:
        """Resume watching a Job that was running before crash."""
        from terrapod.runner.job_manager import watch_job

        result = await watch_job(job_name)

        if result == "succeeded":
            await self._report_status(run_id, "planned")
        else:
            await self._report_status(run_id, "errored", f"Recovered: {result}")


def _handle_signals() -> None:
    """Register signal handlers for graceful shutdown."""
    loop = asyncio.get_event_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _shutdown.set())


async def resolve_run_variables(db, run) -> dict:
    """Resolve variables for a run, split by category."""
    from terrapod.services.variable_service import resolve_variables

    resolved = await resolve_variables(db, run.workspace_id)
    env_vars = [{"key": v.key, "value": v.value} for v in resolved if v.category == "env"]
    terraform_vars = [
        {"key": v.key, "value": v.value} for v in resolved if v.category == "terraform"
    ]
    return {"env": env_vars, "terraform": terraform_vars}


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
