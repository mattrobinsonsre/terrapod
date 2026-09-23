"""The debug linger holds a failed pod open, and nothing else (#1764).

The value of this feature is entirely in when it does NOT fire: a workspace
without debug mode must produce exactly the run it produced before, and a
successful run must never be held. The pod keeps the run's auth token and its
decrypted tfvars while it lingers, so a linger nobody asked for is a real
exposure rather than a harmless delay.
"""

from __future__ import annotations

import pytest

from terrapod.runner import debug_linger


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(debug_linger.ENV_VAR, raising=False)


class TestItDoesNotFire:
    def test_a_successful_run_is_never_held(self, monkeypatch):
        monkeypatch.setenv(debug_linger.ENV_VAR, "600")
        slept: list[int] = []
        assert debug_linger.hold_for_inspection(0, sleep=slept.append) is False
        assert slept == []

    def test_debug_mode_off_means_a_failure_exits_immediately(self):
        slept: list[int] = []
        assert debug_linger.hold_for_inspection(1, sleep=slept.append) is False
        assert slept == []

    def test_a_zero_window_is_off(self, monkeypatch):
        monkeypatch.setenv(debug_linger.ENV_VAR, "0")
        slept: list[int] = []
        assert debug_linger.hold_for_inspection(1, sleep=slept.append) is False
        assert slept == []

    def test_a_malformed_window_is_off_rather_than_fatal(self, monkeypatch):
        """The failure has already been reported by the time this runs.

        A bad value must not change the run's outcome, and must certainly not
        raise out of the orchestrator's last statement.
        """
        monkeypatch.setenv(debug_linger.ENV_VAR, "half an hour")
        slept: list[int] = []
        assert debug_linger.hold_for_inspection(1, sleep=slept.append) is False
        assert slept == []

    def test_a_negative_window_is_off(self, monkeypatch):
        monkeypatch.setenv(debug_linger.ENV_VAR, "-60")
        slept: list[int] = []
        assert debug_linger.hold_for_inspection(1, sleep=slept.append) is False
        assert slept == []


class TestItFires:
    def test_a_failed_run_is_held_for_the_configured_window(self, monkeypatch):
        monkeypatch.setenv(debug_linger.ENV_VAR, "900")
        slept: list[int] = []
        assert debug_linger.hold_for_inspection(1, sleep=slept.append) is True
        assert slept == [900]

    def test_it_says_so_on_stdout_not_only_in_the_logger(self, monkeypatch, capsys):
        """The operator reads the run log, not the structured sink."""
        monkeypatch.setenv(debug_linger.ENV_VAR, "300")
        debug_linger.hold_for_inspection(2, sleep=lambda _s: None)
        out = capsys.readouterr().out
        assert "debug mode" in out
        assert "300" in out
        # It has to be unambiguous that the run already failed, or the message
        # reads as though the run is still going.
        assert "failed" in out

    def test_the_signal_handlers_are_restored(self, monkeypatch):
        """It runs inside the orchestrator, so it must leave no global state."""
        import signal

        monkeypatch.setenv(debug_linger.ENV_VAR, "60")
        before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        debug_linger.hold_for_inspection(1, sleep=lambda _s: None)
        after = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
        assert before == after


class TestTheEntrypointCallsItLast:
    def test_the_hold_comes_after_the_phase_result_is_posted(self):
        """Ordering is the whole contract: report, then hold.

        Held first, an operator inspecting the pod would be looking at a run
        Terrapod still believes is in flight, and the reconciler's stale-run
        timeout would eventually error it out from underneath them.
        """
        import inspect

        from terrapod.runner import job_entrypoint

        src = inspect.getsource(job_entrypoint.main)
        hold = src.index("debug_linger.hold_for_inspection")
        for earlier in ("post_profile", "upload_combined_log"):
            assert src.index(earlier) < hold, (
                f"{earlier} must run before the pod is held — the run has to be "
                "reported before anyone is invited to look at it"
            )
