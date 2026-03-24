"""Tests for runner listener — SSE + polling fallback."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import terrapod.runner.listener as listener_module

RunnerListener = listener_module.RunnerListener


@pytest.fixture(autouse=True)
def fresh_shutdown_event():
    """Replace the module-level shutdown event with one bound to the test loop."""
    new_event = asyncio.Event()
    old_event = listener_module._shutdown
    listener_module._shutdown = new_event
    yield new_event
    listener_module._shutdown = old_event


def _make_listener(shutdown_event: asyncio.Event, **overrides) -> RunnerListener:
    """Create a listener with mocked identity and config."""
    with patch("terrapod.runner.listener.load_runner_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(definitions=[])
        listener = RunnerListener()

    listener.identity = MagicMock()
    listener.identity.listener_id = "test-listener-id"
    listener.identity.api_url = "http://api:8000"
    listener.identity.certificate_pem = "CERT"
    listener._identity_ready = True

    for k, v in overrides.items():
        setattr(listener, k, v)

    return listener


# ── Poll loop ────────────────────────────────────────────────────────


class TestPollLoop:
    async def test_poll_loop_calls_handle_run_available(self, fresh_shutdown_event):
        """Poll loop calls _handle_run_available on each tick."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=0.05)
        call_count = 0

        async def mock_handle():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                fresh_shutdown_event.set()

        listener._handle_run_available = mock_handle

        await asyncio.wait_for(listener._poll_loop(), timeout=2)
        assert call_count >= 3

    async def test_poll_loop_respects_shutdown(self, fresh_shutdown_event):
        """Poll loop exits when shutdown is signaled."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=60)

        # Signal shutdown after a short delay
        async def signal_shutdown():
            await asyncio.sleep(0.05)
            fresh_shutdown_event.set()

        asyncio.create_task(signal_shutdown())
        await asyncio.wait_for(listener._poll_loop(), timeout=2)
        # If we get here, the loop exited properly

    async def test_poll_loop_handles_errors_gracefully(self, fresh_shutdown_event):
        """Poll loop logs errors but continues polling."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=0.05)
        call_count = 0

        async def mock_handle():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("API unreachable")
            fresh_shutdown_event.set()

        listener._handle_run_available = mock_handle

        await asyncio.wait_for(listener._poll_loop(), timeout=2)
        assert call_count >= 3  # Continued despite errors


# ── Handle run available ─────────────────────────────────────────────


class TestHandleRunAvailable:
    async def test_skips_when_at_max_concurrent(self, fresh_shutdown_event):
        """Does not claim if already at max concurrent launches."""
        listener = _make_listener(fresh_shutdown_event, _max_concurrent=2, _active_launches=2)

        with patch("httpx.AsyncClient") as mock_client_cls:
            await listener._handle_run_available()
            mock_client_cls.assert_not_called()

    async def test_handles_no_runs_available(self, fresh_shutdown_event):
        """204 response means no runs — graceful no-op."""
        listener = _make_listener(fresh_shutdown_event)

        mock_response = MagicMock()
        mock_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        listener._http_client = mock_client

        await listener._handle_run_available()

        mock_client.get.assert_called_once()

    async def test_claims_and_launches_run(self, fresh_shutdown_event):
        """Successful claim triggers _launch_run."""
        listener = _make_listener(fresh_shutdown_event)

        run_data = {
            "data": {
                "id": "run-abc12345-abcd-1234-abcd-123456789012",
                "attributes": {"phase": "plan"},
            }
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = run_data
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        listener._http_client = mock_client

        listener._launch_run = AsyncMock()
        await listener._handle_run_available()

        listener._launch_run.assert_called_once()
        assert listener._active_launches == 0  # Decremented after launch


# ── SSE dispatch ─────────────────────────────────────────────────────


class TestDispatchEvent:
    async def test_dispatches_run_available(self, fresh_shutdown_event):
        """run_available event triggers _handle_run_available."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_run_available = AsyncMock()

        await listener._dispatch_event("run_available", json.dumps({"pool_id": "test"}))

        listener._handle_run_available.assert_called_once()

    async def test_dispatches_check_job_status(self, fresh_shutdown_event):
        """check_job_status event triggers _handle_check_job_status."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_check_job_status = AsyncMock()

        data = {"run_id": "r1", "job_name": "j1", "job_namespace": "ns"}
        await listener._dispatch_event("check_job_status", json.dumps(data))

        listener._handle_check_job_status.assert_called_once_with(data)

    async def test_dispatches_cancel_job(self, fresh_shutdown_event):
        """cancel_job event triggers _handle_cancel_job."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_cancel_job = AsyncMock()

        data = {"job_name": "j1", "job_namespace": "ns"}
        await listener._dispatch_event("cancel_job", json.dumps(data))

        listener._handle_cancel_job.assert_called_once_with(data)

    async def test_ignores_invalid_json(self, fresh_shutdown_event):
        """Invalid JSON is logged and skipped."""
        listener = _make_listener(fresh_shutdown_event)
        listener._handle_run_available = AsyncMock()

        await listener._dispatch_event("run_available", "not-json")

        listener._handle_run_available.assert_not_called()

    async def test_ignores_unknown_event(self, fresh_shutdown_event):
        """Unknown event type is ignored."""
        listener = _make_listener(fresh_shutdown_event)
        # Should not raise
        await listener._dispatch_event("unknown_event", json.dumps({"x": 1}))


# ── Concurrent SSE + poll safety ─────────────────────────────────────


class TestConcurrentSafety:
    async def test_sse_and_poll_can_both_trigger_claims(self, fresh_shutdown_event):
        """Both SSE and poll can trigger _handle_run_available concurrently."""
        listener = _make_listener(fresh_shutdown_event, _poll_interval=0.05, _max_concurrent=5)
        claims = []

        async def mock_handle():
            claims.append(1)
            if len(claims) >= 5:
                fresh_shutdown_event.set()

        listener._handle_run_available = mock_handle

        # Run poll loop alongside a simulated SSE trigger
        async def sse_trigger():
            for _ in range(3):
                await asyncio.sleep(0.02)
                if not fresh_shutdown_event.is_set():
                    await listener._handle_run_available()

        await asyncio.wait_for(
            asyncio.gather(listener._poll_loop(), sse_trigger()),
            timeout=2,
        )

        assert len(claims) >= 5  # Both paths contributed
