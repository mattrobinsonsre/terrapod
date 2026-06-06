"""Tests for terrapod.runner.exec_subprocess.

Real-subprocess + real-signal tests. Mocks don't catch the subtle
bugs in signal forwarding — the whole point of porting this from
bash is to make it testable, so the tests run actual children and
send actual SIGTERMs.

Each signal test runs in a fork so the test process isn't itself
affected by the SIGTERM we send to exec_subprocess (which would
prematurely kill pytest if we ran in-process).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from terrapod.runner import exec_subprocess

HELPER_PROG = """\
import signal, sys, time
got = []
def handler(signum, frame):
    got.append(signum)
    if signum == signal.SIGINT:
        # mimic terraform's graceful exit on SIGINT: write a marker and
        # exit 0 after a short pause to prove the SIGINT was honoured.
        time.sleep(0.2)
        sys.stdout.write("interrupt-handled\\n"); sys.stdout.flush()
        sys.exit(0)
signal.signal(signal.SIGINT, handler)
signal.signal(signal.SIGTERM, handler)
sys.stdout.write("ready\\n"); sys.stdout.flush()
time.sleep(30)
"""


def _spawn_exec_subprocess_with_helper(
    helper_path: Path,
    *,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    """Spawn `python -m terrapod.runner.exec_subprocess -- python HELPER`
    as a real subprocess in its own process group, so we can SIGTERM
    it without affecting the test runner."""
    cmd = [
        sys.executable,
        "-m",
        "terrapod.runner.exec_subprocess",
        *(extra_args or []),
        "--",
        sys.executable,
        str(helper_path),
    ]
    # New process group: kill(pid, SIGTERM) goes to exec_subprocess
    # only, not to its child which is in the same group.
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _wait_for_line(proc: subprocess.Popen, needle: str, timeout: float) -> bool:
    """Read proc.stdout one line at a time until `needle` appears or
    `timeout` elapses."""
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    os.set_blocking(proc.stdout.fileno(), False)
    buf = b""
    while time.monotonic() < deadline:
        try:
            chunk = proc.stdout.read(4096)
        except BlockingIOError:
            chunk = None
        if chunk:
            buf += chunk
            if needle.encode() in buf:
                return True
        else:
            time.sleep(0.05)
    return False


class TestRunHappyPath:
    def test_propagates_exit_code(self, tmp_path) -> None:
        helper = tmp_path / "p.py"
        helper.write_text("import sys; sys.exit(7)")
        result = exec_subprocess.run(
            [sys.executable, str(helper)],
            log_file=str(tmp_path / "out.log"),
            tee_to_stdout=False,
        )
        assert result.exit_code == 7
        assert result.signalled is False
        assert result.killed_by_watchdog is False

    def test_captures_stdout_and_stderr_to_log_file(self, tmp_path) -> None:
        helper = tmp_path / "p.py"
        helper.write_text(
            'import sys; sys.stdout.write("hello\\n"); sys.stderr.write("warn\\n"); sys.exit(0)'
        )
        log_path = tmp_path / "out.log"
        result = exec_subprocess.run(
            [sys.executable, str(helper)],
            log_file=str(log_path),
            tee_to_stdout=False,
        )
        assert result.exit_code == 0
        log = log_path.read_text()
        assert "hello" in log
        assert "warn" in log


class TestSignalForwarding:
    def test_sigterm_forwards_sigint_and_child_exits_gracefully(self, tmp_path) -> None:
        """The whole point of the port. Send SIGTERM to our wrapper,
        verify the child receives SIGINT (not SIGTERM, not SIGKILL),
        verify graceful exit with the child's own zero code."""
        helper = tmp_path / "h.py"
        helper.write_text(HELPER_PROG)

        proc = _spawn_exec_subprocess_with_helper(helper)
        try:
            assert _wait_for_line(proc, "ready", timeout=5.0), (
                "child didn't reach ready state within 5s"
            )
            # Send SIGTERM to the wrapper. start_new_session means the
            # child (the helper) is NOT in our process group, so this
            # ONLY hits the wrapper.
            proc.send_signal(signal.SIGTERM)
            stdout = b""
            try:
                stdout, _ = proc.communicate(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail("wrapper didn't exit within 10s after SIGTERM")
            assert b"interrupt-handled" in stdout, (
                f"expected child's SIGINT handler to run; saw stdout={stdout!r}"
            )
            assert proc.returncode == 0, (
                f"expected wrapper to inherit child's exit 0, got {proc.returncode}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_double_sigterm_does_not_re_signal_child(self, tmp_path) -> None:
        """Bash explicitly avoids sending a second INT — it triggers
        terraform's ungraceful path. Verify the wrapper has the same
        guard: a second SIGTERM after the first must NOT propagate."""
        helper = tmp_path / "h.py"
        # Helper records every signal received in a counter file and
        # only exits when SENTINEL_PATH appears.
        sentinel = tmp_path / "exit"
        helper.write_text(
            "import os, signal, sys, time\n"
            f"counter = '{tmp_path}/sig_count'\n"
            f"sentinel = '{sentinel}'\n"
            "open(counter, 'w').write('0')\n"
            "def handler(s, f):\n"
            "    n = int(open(counter).read()) + 1\n"
            "    open(counter, 'w').write(str(n))\n"
            "signal.signal(signal.SIGINT, handler)\n"
            "signal.signal(signal.SIGTERM, handler)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "while not os.path.exists(sentinel):\n"
            "    time.sleep(0.05)\n"
            "sys.exit(0)\n"
        )
        proc = _spawn_exec_subprocess_with_helper(helper)
        try:
            assert _wait_for_line(proc, "ready", timeout=5.0)
            proc.send_signal(signal.SIGTERM)
            time.sleep(0.5)  # give the wrapper's handler time to run
            proc.send_signal(signal.SIGTERM)  # second one should be ignored
            time.sleep(0.5)
            sentinel.touch()
            stdout, _ = proc.communicate(timeout=10.0)
            counter_path = tmp_path / "sig_count"
            count = int(counter_path.read_text())
            assert count == 1, (
                f"expected child to receive exactly 1 forwarded signal; got {count}. "
                f"Sending a second INT to terraform triggers ungraceful abort."
            )
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_watchdog_sigkills_unresponsive_child(self, tmp_path) -> None:
        """If the child doesn't exit within grace, watchdog SIGKILLs it."""
        helper = tmp_path / "h.py"
        # Helper IGNORES SIGINT (mimics a hung terraform).
        helper.write_text(
            "import signal, sys, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        proc = _spawn_exec_subprocess_with_helper(
            helper,
            extra_args=["--child-grace-seconds", "1.0"],
        )
        try:
            assert _wait_for_line(proc, "ready", timeout=5.0)
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            stdout, _ = proc.communicate(timeout=10.0)
            elapsed = time.monotonic() - t0
            # 1.0s grace + small fudge for watchdog scheduling
            assert elapsed < 3.0, f"watchdog took too long: {elapsed:.2f}s"
            # Killed-by-SIGKILL → returncode is -SIGKILL on POSIX, or
            # 128+SIGKILL via shell. Either is acceptable; we just
            # assert it's a non-zero abnormal exit.
            assert proc.returncode != 0, "watchdog kill should produce non-zero exit"
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_natural_exit_does_not_signal(self, tmp_path) -> None:
        """If no SIGTERM ever arrives, signalled stays False."""
        helper = tmp_path / "p.py"
        helper.write_text("print('quick'); import sys; sys.exit(0)")
        result = exec_subprocess.run(
            [sys.executable, str(helper)],
            child_grace_seconds=10.0,
            tee_to_stdout=False,
        )
        assert result.exit_code == 0
        assert result.signalled is False
        assert result.killed_by_watchdog is False


class TestModuleCli:
    def test_missing_dashdash_returns_2(self) -> None:
        """The `--` separator is required so argparse can split our
        options from the child argv. Without it we exit 2 (the
        argparse convention for invocation error)."""
        rc = exec_subprocess.main(argv=[])
        assert rc == 2

    def test_exec_failure_returns_127(self) -> None:
        """Mimics shell convention for `command not found`."""
        with tempfile.TemporaryDirectory() as td:
            absent = Path(td) / "absent-binary"
            rc = exec_subprocess.main(argv=["--", str(absent)])
            assert rc == 127
