"""The listener's job-status report says whether the Job itself has ended (#1649).

``terminal`` is read from the Job's Complete/Failed condition, so the API can
tell a finished Job from one whose pod failed and is being retried. It is
additive: an API that predates it ignores the key, and when the condition
cannot be read the key is simply left out and the status still reported.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import terrapod.runner.listener as listener_module
from tests.services.test_listener import _make_listener


@pytest.fixture(autouse=True)
def _shutdown():
    event = asyncio.Event()
    prior = listener_module._shutdown
    listener_module._shutdown = event
    yield event
    listener_module._shutdown = prior


async def _report(shutdown, status, *, finished=None, finished_exc=None):
    listener = _make_listener(shutdown)
    listener._http_client = MagicMock()
    post = AsyncMock()
    finished_mock = AsyncMock(return_value=finished, side_effect=finished_exc)
    with (
        patch("terrapod.runner.job_manager.get_job_status", AsyncMock(return_value=status)),
        patch("terrapod.runner.job_manager.job_is_finished", finished_mock),
        patch("terrapod.runner.job_manager.get_pod_terminated_info", AsyncMock(return_value=None)),
        patch("terrapod.runner.job_manager.get_job_failure_info", AsyncMock(return_value=None)),
        patch.object(listener_module, "arequest_with_retry", post),
    ):
        await listener._handle_check_job_status(
            {"job_name": "tprun-x-plan", "job_namespace": "ns", "run_id": "r1", "phase": "plan"}
        )
    return post.await_args.kwargs["json"], finished_mock


async def test_a_completed_job_is_terminal(_shutdown):
    body, _ = await _report(_shutdown, "succeeded", finished=True)
    assert body == {"status": "succeeded", "phase": "plan", "terminal": True}


async def test_a_failed_pod_being_retried_is_not_terminal(_shutdown):
    body, _ = await _report(_shutdown, "failed", finished=False)
    assert body["status"] == "failed"
    assert body["terminal"] is False


async def test_a_deleted_job_is_terminal_without_another_lookup(_shutdown):
    body, finished = await _report(_shutdown, None)
    assert body["status"] == "deleted"
    assert body["terminal"] is True
    finished.assert_not_awaited()


async def test_a_running_job_carries_no_flag(_shutdown):
    body, finished = await _report(_shutdown, "running")
    assert "terminal" not in body
    finished.assert_not_awaited()


async def test_an_unreadable_condition_leaves_the_flag_out_and_still_reports(_shutdown):
    body, _ = await _report(_shutdown, "failed", finished_exc=RuntimeError("k8s"))
    assert body["status"] == "failed"
    assert "terminal" not in body
