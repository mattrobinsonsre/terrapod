"""A configure run whose execution environment never becomes ready (#2154).

The Job's own counters call such a pod ACTIVE: a native sidecar that has not
passed its startup probe keeps the pod `Pending`, and `job.status.active`
counts it. So without this the run looks healthy, hangs to the one-hour stale
timeout, and reports "stale" -- which names neither the image nor the reason,
on a reference that came from a workspace setting or an operator's own registry.

Two waits, because they are different events, and getting them the same way
round is the point:

* an image that cannot be pulled, or a container that cannot be created, fails
  NOW -- no amount of waiting changes a wrong reference;
* a sidecar that is up and merely slow to pass its probe gets a grace, because a
  large environment landing on a cold node legitimately takes minutes.

Inverting that pair is the plausible mistake: fail-fast on the slow case tells a
working deployment its image is broken, and grace-then-fail on the broken case
delays a decided failure by ten minutes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.runner.job_manager import _init_stall_of
from terrapod.services.run_reconciler import _check_init_stalled

# `init_stall_detail` is patched on `terrapod.runner.job_manager`, NOT on
# `run_reconciler`: the reconciler imports it lazily inside the function, so the
# name does not exist on that module and patching it there raises
# `AttributeError: ... does not have the attribute`. Patching the source is what
# a lazy import resolves against at call time.


def _run(**kwargs):
    run = MagicMock()
    run.id = "11111111-1111-1111-1111-111111111111"
    run.status = "applying"
    run.job_name = "tprun-abc-apply"
    run.plan_started_at = None
    run.apply_started_at = datetime.now(UTC)
    run.ee_image = "ghcr.io/example/terrapod-ee:2.16"
    for k, v in kwargs.items():
        setattr(run, k, v)
    return run


def _pod(*, phase="Pending", init=()):
    pod = MagicMock()
    pod.status.phase = phase
    pod.status.init_container_statuses = list(init)
    return pod


def _init_status(name, *, waiting=None, running=False, started=None, image="img"):
    cs = MagicMock()
    cs.name = name
    cs.image = image
    cs.started = started
    if waiting is not None:
        cs.state.waiting.reason = waiting
        cs.state.waiting.message = "back-off pulling image"
        cs.state.running = None
    elif running:
        cs.state.waiting = None
        cs.state.running = MagicMock()
    else:
        cs.state.waiting = None
        cs.state.running = None
    return cs


class TestClassification:
    def test_a_pull_failure_is_terminal(self):
        stall = _init_stall_of(
            _pod(init=[_init_status("ee", waiting="ImagePullBackOff", image="x:2.16")])
        )
        assert stall is not None
        terminal, detail = stall
        assert terminal is True
        assert "x:2.16" in detail and "ImagePullBackOff" in detail

    def test_a_running_sidecar_that_has_not_started_is_NOT_terminal(self):
        stall = _init_stall_of(_pod(init=[_init_status("ee", running=True, started=False)]))
        assert stall is not None
        terminal, detail = stall
        assert terminal is False
        assert "startup probe" in detail

    def test_an_ordinary_init_container_is_not_a_stall(self):
        """`started` is None on a plain init container and False only on a
        sidecar whose probe has not passed, so the `is False` is load-bearing:
        `not cs.started` would flag every ordinary init container -- including
        `ca-merge` and `agent-copy`, which are SUPPOSED to be running."""
        assert _init_stall_of(_pod(init=[_init_status("ca-merge", running=True)])) is None

    def test_a_pod_that_is_not_pending_is_never_a_stall(self):
        assert (
            _init_stall_of(
                _pod(phase="Running", init=[_init_status("ee", waiting="ImagePullBackOff")])
            )
            is None
        )

    @pytest.mark.parametrize("reason", ["PodInitializing", "ContainerCreating"])
    def test_progress_reasons_are_deliberately_not_terminal(self, reason):
        """A large environment on a cold node sits in these for minutes.

        Treating them as failure is how a working deployment on a slow registry
        gets told its image is broken.
        """
        assert _init_stall_of(_pod(init=[_init_status("ee", waiting=reason)])) is None


class TestTheReconcilerDecision:
    @patch("terrapod.services.run_reconciler._reap_abandoned_job", new_callable=AsyncMock)
    @patch("terrapod.runner.job_manager.init_stall_detail", new_callable=AsyncMock)
    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_a_terminal_stall_fails_immediately_and_names_the_image(
        self, mock_handle, mock_config, mock_detail, _mock_reap
    ):
        mock_config.return_value = MagicMock(
            ee_startup_timeout_seconds=600, runner_namespace="terrapod-runners"
        )
        mock_detail.return_value = (
            "container 'ee' (image ghcr.io/example/terrapod-ee:2.16): ImagePullBackOff"
        )
        # Started one second ago: nowhere near any grace, and it must still fail.
        run = _run(apply_started_at=datetime.now(UTC))

        await _check_init_stalled(AsyncMock(), run, terminal=True)

        mock_handle.assert_called_once()
        msg = mock_handle.call_args.args[2]
        assert "ghcr.io/example/terrapod-ee:2.16" in msg
        assert "imagePullSecrets" in msg, "the message should say how to fix a private registry"

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_a_slow_sidecar_is_left_alone_inside_the_window(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(
            ee_startup_timeout_seconds=600, runner_namespace="terrapod-runners"
        )
        run = _run(apply_started_at=datetime.now(UTC) - timedelta(seconds=60))

        await _check_init_stalled(AsyncMock(), run, terminal=False)

        mock_handle.assert_not_called()

    @patch("terrapod.services.run_reconciler._reap_abandoned_job", new_callable=AsyncMock)
    @patch("terrapod.runner.job_manager.init_stall_detail", new_callable=AsyncMock)
    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_a_slow_sidecar_fails_past_the_window_naming_the_contract(
        self, mock_handle, mock_config, mock_detail, _mock_reap
    ):
        mock_config.return_value = MagicMock(
            ee_startup_timeout_seconds=600, runner_namespace="terrapod-runners"
        )
        mock_detail.return_value = ""
        run = _run(apply_started_at=datetime.now(UTC) - timedelta(minutes=20))

        await _check_init_stalled(AsyncMock(), run, terminal=False)

        mock_handle.assert_called_once()
        msg = mock_handle.call_args.args[2]
        assert "600s" in msg
        # The probe asserts the image's contract, so an image missing one of the
        # four binaries never passes -- saying so is the difference between an
        # actionable failure and "it timed out".
        for binary in ("ansible-playbook", "ansible-galaxy", "ssh", "git"):
            assert binary in msg, f"the message should name {binary} as part of the contract"
        assert "eeStartupTimeoutSeconds" in msg, "and how to widen the window"

    @patch("terrapod.services.run_reconciler._reap_abandoned_job", new_callable=AsyncMock)
    @patch("terrapod.runner.job_manager.init_stall_detail", new_callable=AsyncMock)
    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_a_run_with_no_image_recorded_still_gets_a_usable_message(
        self, mock_handle, mock_config, mock_detail, _mock_reap
    ):
        """A run created by an API that predates the column carries no image.

        The message must still be actionable rather than naming an empty string
        -- the same N-2 tolerance the Job spec's own defaults provide.
        """
        mock_config.return_value = MagicMock(
            ee_startup_timeout_seconds=600, runner_namespace="terrapod-runners"
        )
        mock_detail.return_value = ""
        run = _run(ee_image="", apply_started_at=datetime.now(UTC) - timedelta(minutes=20))

        await _check_init_stalled(AsyncMock(), run, terminal=False)

        msg = mock_handle.call_args.args[2]
        assert msg.startswith("the Ansible execution environment") or (
            "Ansible execution environment" in msg
        )
        assert "''" not in msg, "an empty image must not be rendered as empty quotes"

    @patch("terrapod.services.run_reconciler._reap_abandoned_job", new_callable=AsyncMock)
    @patch("terrapod.runner.job_manager.init_stall_detail", new_callable=AsyncMock)
    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_the_diagnosis_failing_does_not_mask_the_failure(
        self, mock_handle, mock_config, mock_detail, _mock_reap
    ):
        """Reading the detail is a second Kubernetes call, and it can fail.

        If it did and we let it propagate, the run would stay stalled with
        nothing reported -- the failure mode this whole path exists to remove,
        reintroduced by its own diagnostics.
        """
        mock_config.return_value = MagicMock(
            ee_startup_timeout_seconds=600, runner_namespace="terrapod-runners"
        )
        mock_detail.side_effect = RuntimeError("the apiserver is unreachable")
        run = _run(apply_started_at=datetime.now(UTC) - timedelta(minutes=20))

        await _check_init_stalled(AsyncMock(), run, terminal=False)

        mock_handle.assert_called_once(), "the run must still be errored"
