"""Why a runner phase failed, for the run's error message (#1631)."""

from pathlib import Path

import pytest
import structlog

from terrapod.runner.phases import failure_reason

# What tofu prints for a failed plan (colour on, as the runner runs it): each
# diagnostic framed in box-drawing characters, detail lines after the location.
TOFU_PLAN_FAILURE = (
    "\x1b[0m\x1b[1mRefreshing state...\x1b[0m\n"
    "\x1b[31m╷\x1b[0m\x1b[0m\n"
    "\x1b[31m│\x1b[0m \x1b[0m\x1b[1m\x1b[31mError: \x1b[0m\x1b[0m\x1b[1mUnsupported argument\x1b[0m\n"
    "\x1b[31m│\x1b[0m \x1b[0m\n"
    '\x1b[31m│\x1b[0m \x1b[0m\x1b[0m  on main.tf line 3, in resource "random_pet" "demo":\n'
    "\x1b[31m│\x1b[0m \x1b[0m   3:   lenght = 2\n"
    "\x1b[31m│\x1b[0m \x1b[0m\n"
    '\x1b[31m│\x1b[0m \x1b[0mAn argument named "lenght" is not expected here. Did you mean "length"?\n'
    "\x1b[31m╵\x1b[0m\x1b[0m\n"
)


@pytest.fixture(autouse=True)
def _forget_errors():
    failure_reason.reset()
    yield
    failure_reason.reset()


def _log(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


class TestTofuErrors:
    def test_summary_and_location_without_colour_or_frame(self, tmp_path):
        log = _log(tmp_path, "plan.log", TOFU_PLAN_FAILURE)
        assert failure_reason.tofu_errors(log) == [
            "Error: Unsupported argument (on main.tf line 3)"
        ]

    def test_detail_lines_are_never_taken(self, tmp_path):
        # Detail lines can quote values; only the summary and location count.
        log = _log(tmp_path, "plan.log", TOFU_PLAN_FAILURE)
        assert "not expected here" not in "\n".join(failure_reason.tofu_errors(log))
        assert "lenght = 2" not in "\n".join(failure_reason.tofu_errors(log))

    def test_an_error_without_a_location(self, tmp_path):
        log = _log(
            tmp_path,
            "init.log",
            "╷\n│ Error: Failed to query available provider packages\n│\n│ Could not retrieve the list\n╵\n",
        )
        assert failure_reason.tofu_errors(log) == [
            "Error: Failed to query available provider packages"
        ]

    def test_at_most_three_distinct_errors(self, tmp_path):
        blocks = "".join(
            f"╷\n│ Error: Problem {n}\n│\n│   on main.tf line {n}:\n╵\n" for n in (1, 2, 2, 3, 4)
        )
        log = _log(tmp_path, "plan.log", blocks)
        assert failure_reason.tofu_errors(log) == [
            "Error: Problem 1 (on main.tf line 1)",
            "Error: Problem 2 (on main.tf line 2)",
            "Error: Problem 3 (on main.tf line 3)",
        ]

    def test_a_clean_log_or_a_missing_file_has_none(self, tmp_path):
        assert failure_reason.tofu_errors(_log(tmp_path, "plan.log", "Plan: 1 to add.\n")) == []
        assert failure_reason.tofu_errors(tmp_path / "missing.log") == []

    def test_a_warning_is_not_an_error(self, tmp_path):
        log = _log(tmp_path, "plan.log", "╷\n│ Warning: Deprecated attribute\n╵\n")
        assert failure_reason.tofu_errors(log) == []


class TestLastLoggedError:
    def _emit(self, method: str, event: str, **kw):
        failure_reason.remember_errors(None, method, {"event": event, **kw})

    def test_nothing_logged(self):
        assert failure_reason.last_logged_error() is None

    def test_event_with_err(self):
        self._emit("error", "configuration archive unusable", err="not a readable tar.gz")
        assert (
            failure_reason.last_logged_error()
            == "configuration archive unusable: not a readable tar.gz"
        )

    def test_hook_and_exit_code_are_named(self):
        self._emit("error", "pre_plan hook failed", hook="lint", rc=2)
        assert failure_reason.last_logged_error() == "pre_plan hook failed (hook=lint, rc=2)"

    def test_the_latest_error_wins_and_warnings_do_not_count(self):
        self._emit("error", "first")
        self._emit("error", "second")
        self._emit("warning", "plan failed", rc=1)
        assert failure_reason.last_logged_error() == "second"

    def test_it_is_a_working_structlog_processor(self):
        structlog.configure(
            processors=[failure_reason.remember_errors, structlog.processors.KeyValueRenderer()]
        )
        try:
            structlog.get_logger("t").error("init failed", rc=1)
        finally:
            structlog.reset_defaults()
        assert failure_reason.last_logged_error() == "init failed (rc=1)"


class TestFailureReason:
    def test_a_clean_exit_has_no_reason(self, tmp_path):
        failure_reason.remember_errors(None, "error", {"event": "ignored"})
        assert (
            failure_reason.failure_reason(0, [_log(tmp_path, "plan.log", TOFU_PLAN_FAILURE)])
            is None
        )

    def test_tofu_errors_come_first(self, tmp_path):
        failure_reason.remember_errors(None, "error", {"event": "init failed", "rc": 1})
        logs = [tmp_path / "apply.log", _log(tmp_path, "plan.log", TOFU_PLAN_FAILURE)]
        assert (
            failure_reason.failure_reason(1, logs)
            == "Error: Unsupported argument (on main.tf line 3)"
        )

    def test_the_latest_phase_with_errors_is_reported(self, tmp_path):
        init = _log(tmp_path, "init.log", "╷\n│ Error: Init problem\n╵\n")
        plan = _log(tmp_path, "plan.log", "╷\n│ Error: Plan problem\n╵\n")
        assert failure_reason.failure_reason(1, [plan, init]) == "Error: Plan problem"

    def test_otherwise_the_runners_last_error(self, tmp_path):
        failure_reason.remember_errors(
            None, "error", {"event": "pre_plan hook failed", "hook": "lint", "rc": 2}
        )
        logs = [_log(tmp_path, "plan.log", ""), tmp_path / "init.log"]
        assert failure_reason.failure_reason(1, logs) == "pre_plan hook failed (hook=lint, rc=2)"

    def test_an_unknown_cause_has_no_reason(self, tmp_path):
        assert failure_reason.failure_reason(1, [tmp_path / "plan.log"]) is None

    def test_the_reason_is_bounded(self, tmp_path):
        failure_reason.remember_errors(None, "error", {"event": "x" * 5000})
        reason = failure_reason.failure_reason(1, [])
        assert len(reason) == failure_reason.MAX_REASON_CHARS
        assert reason.endswith("…")
