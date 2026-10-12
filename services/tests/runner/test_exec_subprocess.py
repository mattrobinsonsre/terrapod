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


class TestThePlatformsOwnEnvironmentIsNotHandedToTheEngine:
    """`TP_*` is scrubbed from the child's environment.

    `TP_AUTH_TOKEN` is the run's runner token — it uploads this run's
    artifacts and its state. Without `env=`, the engine inherited it, and so
    did every provider plugin the engine loads. A provider runs third-party
    code against the operator's cloud credentials by design; it has no
    business also holding the token that writes the state.

    A scrub, not an allowlist: the engine needs `PATH`, `HOME`, every `TF_*`,
    the cloud credential variables and the proxy and CA-bundle variables that
    libraries read from nowhere else. An allowlist would have to enumerate all
    of that and would break an egress-proxied deployment the first time it
    missed one — so this pins both directions.

    Runs a real child that dumps its own environment, in keeping with the rest
    of this file: a mocked `Popen` would assert on the kwargs we passed rather
    than on what the process actually received.
    """

    def test_tp_is_dropped_and_everything_else_survives(self, monkeypatch):
        monkeypatch.setenv("TP_AUTH_TOKEN", "runtok-should-not-appear")
        monkeypatch.setenv("TP_API_URL", "https://api.internal")
        # Lower case is the same attempt; `is_reserved_env_key` folds case.
        monkeypatch.setenv("tp_auth_token", "runtok-lower-should-not-appear")
        for k, v in {
            "TF_IN_AUTOMATION": "1",
            "HTTPS_PROXY": "http://egress:3128",
            "NO_PROXY": "10.0.0.0/8",
            "SSL_CERT_FILE": "/etc/ssl/ca.pem",
            "AWS_ROLE_ARN": "arn:aws:iam::1:role/r",
        }.items():
            monkeypatch.setenv(k, v)

        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "env.log")
            result = exec_subprocess.run(["/usr/bin/env"], log_file=log)
            assert result.exit_code == 0
            with open(log, encoding="utf-8", errors="replace") as fh:
                dumped = fh.read()

        names = {line.split("=", 1)[0] for line in dumped.splitlines() if "=" in line}

        leaked = sorted(n for n in names if n.upper().startswith("TP_"))
        assert not leaked, f"platform plumbing reached the engine: {leaked}"
        assert "runtok-should-not-appear" not in dumped
        assert "runtok-lower-should-not-appear" not in dumped

        for keep in (
            "TF_IN_AUTOMATION",
            "HTTPS_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "AWS_ROLE_ARN",
            "PATH",
        ):
            assert keep in names, f"{keep} was dropped — the engine needs it"


class TestTheLogStreamRedactsRegisteredSecrets:
    """A cloud identity token must not survive into the run log (#2047).

    Every other secret the runner holds is kept out of the stream by MECHANISM --
    0600 files, no argv, `TP_*` scrubbed from the child environment -- and that is
    the better guarantee. This one cannot be: we hand the token to a provider on
    purpose, and terraform-provider-aws logs the STS request body carrying it at
    `TF_LOG=DEBUG`. So the literal value is stripped on the way through.
    """

    TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6InRlc3Qifq." + ("A" * 300) + ".sIgNaTuRe-_x"

    @pytest.fixture(autouse=True)
    def _clean(self):
        exec_subprocess._reset_secrets_for_tests()
        yield
        exec_subprocess._reset_secrets_for_tests()

    def _emit(self, tmp_path, payload: str) -> str:
        """Run a child that prints `payload`, return what reached the log file."""
        helper = tmp_path / "p.py"
        helper.write_text(
            "import sys\nsys.stdout.write(open(sys.argv[1], encoding='utf-8').read())\n"
        )
        data = tmp_path / "payload.txt"
        data.write_text(payload, encoding="utf-8")
        log = tmp_path / "out.log"
        result = exec_subprocess.run(
            [sys.executable, str(helper), str(data)],
            log_file=str(log),
            tee_to_stdout=False,
        )
        assert result.exit_code == 0
        return log.read_text(encoding="utf-8", errors="replace")

    def test_an_unregistered_run_is_byte_identical(self, tmp_path) -> None:
        """The overwhelming majority of runs. With nothing registered the bytes go
        through unexamined, so a deployment not using per-workspace cloud identity
        pays nothing and cannot have its output altered."""
        payload = f"terraform plan\nWebIdentityToken={self.TOKEN}\ndone\n"
        assert self._emit(tmp_path, payload) == payload

    def test_a_registered_token_does_not_reach_the_log(self, tmp_path) -> None:
        exec_subprocess.register_secret(self.TOKEN)
        out = self._emit(tmp_path, f"calling sts\nWebIdentityToken={self.TOKEN}\nok\n")

        assert self.TOKEN not in out
        assert "[redacted: cloud identity token]" in out
        # The surrounding output survives -- a redactor that ate the log would be
        # a worse failure than the one it prevents.
        assert "calling sts" in out
        assert "ok" in out

    def test_a_token_STRADDLING_a_read_boundary_is_still_removed(self, tmp_path) -> None:
        """The case the whole carry buffer exists for, and the one a per-chunk
        replace misses -- silently, while looking like it works.

        The child is read in 4096-byte chunks, so the payload is padded to put the
        token across that boundary. Several offsets, because landing exactly on it
        is the interesting one and a single offset might miss by luck.
        """
        exec_subprocess.register_secret(self.TOKEN)
        chunk = 4096
        for offset in (1, 10, len(self.TOKEN) // 2, len(self.TOKEN) - 1):
            pad = "x" * (chunk - offset)
            out = self._emit(tmp_path, f"{pad}{self.TOKEN}TAIL\n")
            assert self.TOKEN not in out, f"token survived at offset {offset}"
            assert "TAIL" in out, f"output truncated at offset {offset}"

    def test_a_token_wholly_inside_the_held_back_tail_is_removed(self, tmp_path) -> None:
        """The flush after the loop. A token in the last bytes of the stream is
        never followed by another chunk, so without the final `_redact` it would
        be emitted verbatim -- and the end of a log is exactly where a provider's
        last error prints its request."""
        exec_subprocess.register_secret(self.TOKEN)
        out = self._emit(tmp_path, f"start\nWebIdentityToken={self.TOKEN}")

        assert self.TOKEN not in out
        assert "start" in out

    def test_several_tokens_are_all_removed(self, tmp_path) -> None:
        """A run mints one token per provider configuration, so more than one is
        the normal case rather than the exotic one."""
        second = self.TOKEN.replace("sIgNaTuRe", "oThErSiG")
        exec_subprocess.register_secret(self.TOKEN)
        exec_subprocess.register_secret(second)
        out = self._emit(tmp_path, f"a={self.TOKEN}\nb={second}\n")

        assert self.TOKEN not in out
        assert second not in out
        assert out.count("[redacted: cloud identity token]") == 2

    def test_a_short_value_is_SKIPPED_rather_than_registered(self) -> None:
        """A short string would match ordinary output and mangle the log, which is
        worse than the leak it would prevent.

        Skipped, not raised. Raising was the first shape and it put the power to
        FAIL A RUN inside a log-hygiene mechanism: the only caller hands over
        whatever the API returned, so a short or malformed token would have
        aborted a run that was otherwise fine. Nothing of value is lost, because
        a value this short cannot be a JWT and so is not the credential this
        exists to hide.
        """
        assert exec_subprocess.register_secret("short") is False
        assert exec_subprocess.register_secret("") is False
        assert exec_subprocess._SECRETS == set(), "a short value was registered anyway"

    def test_a_short_value_does_not_turn_the_redactor_on(self, tmp_path) -> None:
        """The property that matters about the skip: output stays byte-identical.

        If a skipped value left the machinery armed, `overlap` would be computed
        from it and the stream would be chunked and re-assembled for nothing --
        and a one-character value would match everywhere.
        """
        exec_subprocess.register_secret("short")
        payload = "short short short\nplan complete\n"
        assert self._emit(tmp_path, payload) == payload

    def test_a_long_value_reports_that_it_registered(self) -> None:
        assert exec_subprocess.register_secret(self.TOKEN) is True

    def test_registration_is_idempotent(self) -> None:
        """The credential phase can run more than once in a process in principle,
        and a duplicate must not double the held-back tail."""
        exec_subprocess.register_secret(self.TOKEN)
        exec_subprocess.register_secret(self.TOKEN)
        assert len(exec_subprocess._SECRETS) == 1


class TestTheHeldBackTailIsBoundedInTimeAsWellAsBytes:
    """`_split_tail`: a slow stream must not go silent for minutes (#2051).

    Holding back `longest secret - 1` BYTES is what makes a straddling secret
    impossible, and on its own it is badly behaved in time. `bufsize=0` makes a
    read one syscall returning whatever is available -- typically one line --
    so with an ~850-byte identity token a `tofu apply` emitting one short
    "Still creating..." line every ten seconds would hold back seventeen lines,
    nearly three minutes of silence. That is the log someone watches to see
    where a slow apply is stuck, and it is every run in the cloud-identity
    cohort.
    """

    OVERLAP = 849  # a realistic token length minus one

    def test_a_slow_line_stream_emits_each_line_as_it_arrives(self):
        """The regression this exists for. Byte-bounded, these lines are held."""
        line = b"aws_db_instance.x: Still creating... [10s elapsed]\n"
        send, carry = exec_subprocess._split_tail(line, overlap=self.OVERLAP, line_safe=True)
        assert send == line, "a complete line must go out, not wait for 849 bytes"
        assert carry == b""

    def test_byte_bounded_the_same_line_is_held(self):
        """The contrast, so the test above is not passing for some other
        reason: with a newline-bearing secret registered there is no safe cut
        and the line is correctly held back."""
        line = b"aws_db_instance.x: Still creating... [10s elapsed]\n"
        send, carry = exec_subprocess._split_tail(line, overlap=self.OVERLAP, line_safe=False)
        assert send == b""
        assert carry == line

    def test_a_partial_line_is_still_held(self):
        """Cutting at the newline must not emit an incomplete trailing line --
        a secret could still be forming in it."""
        send, carry = exec_subprocess._split_tail(
            b"done\npartial-", overlap=self.OVERLAP, line_safe=True
        )
        assert send == b"done\n"
        assert carry == b"partial-"

    def test_a_single_line_longer_than_the_overlap_still_respects_the_byte_bound(self):
        """A stream with no newlines at all -- or one very long line -- must not
        grow the carry without limit, and must still hold back enough that a
        secret cannot straddle."""
        buf = b"x" * (self.OVERLAP + 100)
        send, carry = exec_subprocess._split_tail(buf, overlap=self.OVERLAP, line_safe=True)
        assert len(carry) == self.OVERLAP
        assert send == buf[:100]

    def test_nothing_registered_emits_everything(self):
        """`overlap` is 0 when no secret is registered, so the bytes go straight
        through -- the property that makes an unaffected deployment pay
        nothing."""
        buf = b"no newline here"
        send, carry = exec_subprocess._split_tail(buf, overlap=0, line_safe=True)
        assert send == buf
        assert carry == b""

    def test_the_split_never_loses_or_duplicates_a_byte(self):
        """The invariant the whole loop depends on. Checked across both modes
        and several shapes, because a flush that drops the tail loses the end
        of the log -- where a provider's last error prints."""
        for buf in (
            b"",
            b"\n",
            b"a",
            b"a\nb",
            b"x" * 5000,
            b"line\n" * 300,
            b"head\n" + b"y" * 2000,
        ):
            for line_safe in (True, False):
                for overlap in (0, 1, self.OVERLAP):
                    send, carry = exec_subprocess._split_tail(
                        buf, overlap=overlap, line_safe=line_safe
                    )
                    assert send + carry == buf, (buf[:20], line_safe, overlap)
