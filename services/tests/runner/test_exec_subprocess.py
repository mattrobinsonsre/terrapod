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
import random
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


class TestAnExplicitChildEnvironment:
    """`env=` replaces the child's environment instead of extending it (#1931).

    `os.environ.update` can only ADD, and the Ansible collection phase has to
    REMOVE: an inherited `ANSIBLE_GALAXY_SERVER_LIST` would redirect where a
    run's collections come from, which is a supply-chain decision and not the
    ambient environment's to make. So the caller composes the whole environment
    and hands it over.

    Driven through a real child that dumps its own environment, like the class
    above: an optional parameter makes the BYPASS the default, so every
    pre-existing test here exercises the `env=None` path and a green suite would
    say nothing about this one. That is how a total VCS outage shipped in v1.3.0
    (#1244) — the wrapper was reached through an optional argument no test
    supplied.
    """

    def _dump(self, env=None) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "env.log")
            result = exec_subprocess.run(["/usr/bin/env"], log_file=log, env=env)
            assert result.exit_code == 0, result.exit_code
            with open(log, encoding="utf-8", errors="replace") as fh:
                dumped = fh.read()
        out = {}
        for line in dumped.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def test_the_child_gets_exactly_what_was_passed(self):
        got = self._dump({"PATH": os.environ.get("PATH", ""), "ONLY_THIS": "yes"})
        assert got.get("ONLY_THIS") == "yes"

    def test_an_inherited_variable_is_ABSENT_when_it_is_left_out(self, monkeypatch):
        """The whole reason the parameter exists. `os.environ.update` cannot do
        this, and the galaxy server settings are the case that needs it."""
        monkeypatch.setenv("ANSIBLE_GALAXY_SERVER_LIST", "evil")
        inherited = self._dump()
        assert inherited.get("ANSIBLE_GALAXY_SERVER_LIST") == "evil"

        replaced = self._dump({"PATH": os.environ.get("PATH", "")})
        assert "ANSIBLE_GALAXY_SERVER_LIST" not in replaced

    def test_reserved_keys_are_still_filtered_from_an_explicit_environment(self):
        """`env=` must not become a way to smuggle the run's own token into a
        child: the reserved-key filter applies to it exactly as it does to
        `os.environ`, or this parameter would be a hole in the scrub the class
        above exists to pin."""
        got = self._dump(
            {
                "PATH": os.environ.get("PATH", ""),
                "TP_AUTH_TOKEN": "runtok-should-not-appear",
                "tp_api_url": "https://api.internal",
                "KEPT": "1",
            }
        )
        assert "TP_AUTH_TOKEN" not in got
        assert "tp_api_url" not in got
        assert got.get("KEPT") == "1"

    def test_omitting_it_still_inherits_this_process(self, monkeypatch):
        """The default path, pinned so the parameter cannot change it."""
        monkeypatch.setenv("SOME_INHERITED_THING", "inherited")
        assert self._dump().get("SOME_INHERITED_THING") == "inherited"


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
    """`_split_tail`: a slow stream must not go silent for minutes (#2051, #2145).

    Something must be held back between reads or a secret straddling a chunk
    boundary survives -- silently, while the redactor looks as though it worked.
    The question is WHAT, and bytes are the wrong unit: `bufsize=0` makes a read
    one syscall returning whatever is available, typically a single line, so
    holding a fixed `longest secret - 1` bytes meant a stream emitting one short
    line every ten seconds sat silent for minutes. That is the log someone
    watches to find where a slow apply is stuck.

    #2051 fixed it by cutting at the last newline, but only while no registered
    secret contained one -- so a multi-line secret (an SSH machine credential,
    #1933) took the whole run back to the byte bound. Measured at the time:
    nothing emitted at all in 120 seconds.

    #2145 holds back only the longest suffix that is a proper PREFIX of a
    secret, which needs no assumption about newlines. So there is no mode to
    fall out of, and these tests pin both halves: output is never delayed, and a
    secret split anywhere is still removed.
    """

    #: A realistic single-line credential -- the identity token (#2047).
    TOKEN = b"eyJhbGciOiJSUzI1NiJ9." + b"A" * 800 + b".sIgNaTuRe"

    #: A realistic MULTI-LINE credential -- an SSH machine credential (#1933).
    #: This is the shape that defeated the newline cut, so every latency
    #: assertion below is made with it registered rather than with the token.
    KEY = (
        b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + b"\n".join([b"b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB"] * 45)
        + b"\n-----END OPENSSH PRIVATE KEY-----\n"
    )

    def _split(self, buf: bytes, *secrets: bytes):
        """`_split_tail` with the loop's own window rule, not a copy of it."""
        secrets = tuple(secrets)
        return exec_subprocess._split_tail(
            buf, secrets=secrets, ceiling=exec_subprocess._scan_ceiling(secrets)
        )

    def _drive(self, chunks, *secrets: bytes) -> bytes:
        """Everything a viewer would ever see, driving the loop's own steps.

        Models `run`'s read loop -- redact, split, emit, flush the carry at EOF
        -- so a streaming property can be asserted without a subprocess per
        case. The real loop is driven end to end by
        `test_a_line_reaches_the_log_while_the_child_is_still_running`, which is
        what stops this helper from being the only evidence.
        """
        secrets = tuple(secrets)
        ceiling = exec_subprocess._scan_ceiling(secrets)
        for secret in secrets:
            exec_subprocess.register_secret(secret.decode())
        carry, seen = b"", b""
        try:
            for chunk in chunks:
                buf = exec_subprocess._redact(carry + chunk)
                send, carry = exec_subprocess._split_tail(buf, secrets=secrets, ceiling=ceiling)
                seen += send
            return seen + carry
        finally:
            exec_subprocess._reset_secrets_for_tests()

    @pytest.fixture(autouse=True)
    def _clean(self):
        exec_subprocess._reset_secrets_for_tests()
        yield
        exec_subprocess._reset_secrets_for_tests()

    # ---- the latency half -------------------------------------------------

    def test_a_complete_line_goes_out_with_a_multi_line_secret_registered(self):
        """The regression this exists for, in the case that used to fail.

        Under #2051's newline cut this line was held back in full, because a
        key containing newlines made every cut unsafe.
        """
        line = b"aws_db_instance.x: Still creating... [10s elapsed]\n"
        send, carry = self._split(line, self.KEY)
        assert send == line, "a complete line must not wait for a key-sized buffer"
        assert carry == b""

    def test_an_ordinary_line_holds_back_nothing_at_all(self):
        """Not merely 'less': ordinary output has no secret forming in it, so
        the correct hold-back is zero bytes and that is what makes the stream
        continuous rather than just faster."""
        for line in (
            b"ok: [web1]\n",
            b"TASK [install packages] ****\n",
            b"changed: [db1] => {}\n",
            b"no trailing newline at all",
        ):
            send, carry = self._split(line, self.KEY, self.TOKEN)
            assert carry == b"", line

    def test_a_sparse_stream_emits_every_line_as_it_arrives(self):
        """Twelve reads ten seconds apart: every one must come out on arrival.

        Measured before the fix with a 3,175-byte key registered: zero of the
        twelve were emitted.
        """
        chunks = [f"changed: [host{i:02d}]\n".encode() for i in range(12)]
        carry, emitted = b"", 0
        ceiling = exec_subprocess._scan_ceiling((self.KEY,))
        for chunk in chunks:
            buf = exec_subprocess._redact(carry + chunk)
            send, carry = exec_subprocess._split_tail(buf, secrets=(self.KEY,), ceiling=ceiling)
            if send:
                emitted += 1
        assert emitted == 12
        assert carry == b""

    def test_the_hold_back_is_exactly_the_bytes_that_opened_a_secret(self):
        """The contrast that stops the tests above passing for the wrong reason:
        when the tail really is a secret in the making, it IS held -- and only
        it, not the line in front of it."""
        opening = self.KEY[:40]
        buf = b"ok: [web1]\n" + opening
        send, carry = self._split(buf, self.KEY)
        assert send == b"ok: [web1]\n"
        assert carry == opening

    def test_only_the_bytes_that_could_OPEN_a_secret_are_held(self):
        """#2051 had to hold any incomplete trailing line, because a secret
        could be forming in it. Matching on prefixes answers that question
        directly, byte by byte -- which is more precise than it first looks, and
        the first case here is why the assertion is written out rather than
        assumed: the key begins `-----BEGIN`, so a line ending in a hyphen holds
        exactly that one hyphen back and emits everything in front of it."""
        send, carry = self._split(b"done\npartial-", self.KEY)
        assert send == b"done\npartial"
        assert carry == b"-"

        # A tail that cannot open either secret holds nothing at all.
        send, carry = self._split(b"done\npartial!", self.KEY, self.TOKEN)
        assert send == b"done\npartial!"
        assert carry == b""

    # ---- the correctness half ---------------------------------------------

    def test_a_token_straddling_every_byte_boundary_is_removed(self):
        """Every split point, not a representative one: the straddle is the
        whole reason anything is held back."""
        payload = b"before " + self.TOKEN + b" after\n"
        for cut in range(1, len(self.TOKEN)):
            at = len(b"before ") + cut
            seen = self._drive([payload[:at], payload[at:]], self.TOKEN)
            assert self.TOKEN not in seen, f"leaked when split at byte {cut}"

    def test_a_multi_line_key_arriving_in_tiny_chunks_is_removed(self):
        """A read can return a single byte. The key is 3 KB and spans 46 lines,
        so this is the case with the most boundaries to get wrong."""
        data = b"log line\n" + self.KEY + b"\ntrailing\n"
        for size in (1, 2, 3, 7, 64, 1000):
            chunks = [data[i : i + size] for i in range(0, len(data), size)]
            seen = self._drive(chunks, self.KEY)
            assert self.KEY not in seen, f"leaked at chunk size {size}"
            assert b"log line\n" in seen and b"trailing\n" in seen

    def test_randomised_interleavings_never_leak(self):
        """Fixed seed, so a failure is reproducible rather than a story about a
        run nobody can repeat."""
        rng = random.Random(7)
        pieces = [b"changed: [h1]\n", b"ok: [h2]\n", self.KEY, self.TOKEN, b"\n"]
        for trial in range(200):
            data = b"".join(rng.choice(pieces) for _ in range(6))
            size = rng.choice([1, 4, 17, 4096])
            chunks = [data[i : i + size] for i in range(0, len(data), size)]
            seen = self._drive(chunks, self.KEY, self.TOKEN)
            assert self.KEY not in seen, f"key leaked in trial {trial}"
            assert self.TOKEN not in seen, f"token leaked in trial {trial}"

    def test_a_secret_is_held_until_it_is_whole_then_never_appears(self):
        """Byte at a time: the arrived-so-far bytes are retained every step, so
        there is no moment at which half a secret has been emitted."""
        chunks = [self.KEY[i : i + 1] for i in range(len(self.KEY))]
        seen = self._drive(chunks, self.KEY)
        assert self.KEY not in seen
        assert seen == exec_subprocess._REDACTED

    # ---- the invariants ---------------------------------------------------

    def test_the_carry_never_exceeds_the_scan_ceiling(self):
        """What bounds memory now that the hold-back is content-dependent. A
        single very long line used to be the reason for the byte bound; it is
        now the reason for the ceiling."""
        secrets = (self.KEY, self.TOKEN)
        ceiling = exec_subprocess._scan_ceiling(secrets)
        for buf in (
            b"x" * 10000,
            b"x" * 10000 + b"\n",
            self.KEY[:-1],
            b"-" * 5000,
            b"e" * 5000,
        ):
            _, carry = exec_subprocess._split_tail(buf, secrets=secrets, ceiling=ceiling)
            assert len(carry) <= ceiling, (buf[:12], len(carry), ceiling)

    def test_a_long_secret_free_line_is_emitted_whole(self):
        """The old byte bound held back 849 bytes of any such line. Nothing in
        10 KB of `x` can open a key or a JWT, so all of it goes out."""
        buf = b"x" * 10000 + b"\n"
        send, carry = self._split(buf, self.KEY, self.TOKEN)
        assert send == buf
        assert carry == b""

    def test_nothing_registered_emits_everything(self):
        """The property that makes an unaffected deployment pay nothing."""
        buf = b"no newline here"
        send, carry = self._split(buf)
        assert send == buf
        assert carry == b""

    def test_the_split_never_loses_or_duplicates_a_byte(self):
        """The invariant the whole loop depends on: a flush that drops the tail
        loses the end of the log, where a provider's last error prints."""
        for buf in (
            b"",
            b"\n",
            b"a",
            b"a\nb",
            b"x" * 5000,
            b"line\n" * 300,
            b"head\n" + b"y" * 2000,
            self.KEY[:50],
            b"tail-" + self.TOKEN[:30],
        ):
            for secrets in ((), (self.TOKEN,), (self.KEY, self.TOKEN)):
                send, carry = exec_subprocess._split_tail(
                    buf, secrets=secrets, ceiling=exec_subprocess._scan_ceiling(secrets)
                )
                assert send + carry == buf, (buf[:20], len(secrets))


class TestTheStreamIsContinuousThroughTheRealLoop:
    """The property asserted against `run` itself, not a model of it (#2145).

    Every test above drives `_split_tail` or a helper that mimics the read loop.
    That is where the logic is, but it cannot see the loop being wired up
    wrongly -- a byte bound reintroduced in `run`, or a carry flushed only at
    exit. So this watches a real log file fill while the child is still alive.

    Driven as a REAL SUBPROCESS rather than a thread, like every other
    end-to-end test in this file: `run` installs SIGINT/SIGTERM handlers, and
    `signal.signal` raises `ValueError: signal only works in main thread of the
    main interpreter` off the main thread.
    """

    KEY = TestTheHeldBackTailIsBoundedInTimeAsWellAsBytes.KEY

    @pytest.fixture(autouse=True)
    def _clean(self):
        exec_subprocess._reset_secrets_for_tests()
        yield
        exec_subprocess._reset_secrets_for_tests()

    def test_a_line_reaches_the_log_while_the_child_is_still_running(self, tmp_path):
        """A multi-line secret registered, and a child that prints then waits.

        The first line must be readable while the child is still alive. It is
        the only assertion in this file about WHEN output appears rather than
        what it contains, and it is the one a byte-bounded implementation fails:
        with a 3 KB key registered it would hold the line until 3 KB had
        accumulated, which never happens here.

        **The child waits for a file rather than for a fixed time.** A first
        version slept four seconds and gave the poll three, which passed
        locally in 4.19s and failed in CI -- a deadline in the low single digits
        of seconds is a flake waiting for a loaded runner, and the window has to
        outlast interpreter startup, an import and two process spawns. Gating on
        a file the test creates makes the window as long as the test needs, so
        the only timeout left is the generous upper bound on a line that never
        comes, which is a real failure rather than a slow machine.
        """
        secret = tmp_path / "secret.txt"
        secret.write_bytes(self.KEY)
        release = tmp_path / "release"

        child = tmp_path / "slow.py"
        child.write_text(
            "import os, sys, time\n"
            "sys.stdout.write('FIRST LINE\\n')\n"
            "sys.stdout.flush()\n"
            "# Wait to be released, with a cap so a broken test cannot hang CI.\n"
            "for _ in range(600):\n"
            "    if os.path.exists(sys.argv[1]):\n"
            "        break\n"
            "    time.sleep(0.1)\n"
            "sys.stdout.write('LAST LINE\\n')\n"
        )

        log = tmp_path / "out.log"
        driver = tmp_path / "driver.py"
        driver.write_text(
            "import sys\n"
            "from terrapod.runner import exec_subprocess\n"
            "with open(sys.argv[1], 'rb') as fh:\n"
            "    assert exec_subprocess.register_secret(fh.read().decode())\n"
            "r = exec_subprocess.run(\n"
            "    [sys.executable, sys.argv[2], sys.argv[3]],\n"
            "    log_file=sys.argv[4],\n"
            "    tee_to_stdout=False,\n"
            ")\n"
            "sys.exit(r.exit_code)\n"
        )

        # `terrapod` on the driver's path, DERIVED from the module under test
        # rather than assumed. A script run as `python /tmp/.../driver.py` gets
        # its own directory on `sys.path` and NOT the working directory, so the
        # first version imported successfully only where the package happens to
        # be installed -- true of an editable venv, false in the test image,
        # where the tree is on the path because that is where pytest was
        # invoked. It failed in CI with `ModuleNotFoundError: No module named
        # 'terrapod'` and nothing else.
        package_root = Path(exec_subprocess.__file__).parents[2]
        env = {**os.environ, "PYTHONPATH": str(package_root)}

        proc = subprocess.Popen(
            [
                sys.executable,
                str(driver),
                str(secret),
                str(child),
                str(release),
                str(log),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=env,
        )

        def _explain(note: str) -> str:
            """Whatever the driver said, so a CI failure is diagnosable.

            The first version asserted before ever reading these pipes, so its
            CI failure carried no information about the cause at all.
            """
            try:
                proc.kill()
                out, err = proc.communicate(timeout=10)
            except Exception:  # pragma: no cover - diagnostics only
                out = err = b"<unavailable>"
            return (
                f"{note}\n"
                f"driver rc={proc.returncode}\n"
                f"driver stdout={out.decode(errors='replace')[-500:]!r}\n"
                f"driver stderr={err.decode(errors='replace')[-800:]!r}\n"
                f"log exists={log.exists()} "
                f"bytes={log.stat().st_size if log.exists() else 0}"
            )

        deadline = time.monotonic() + 30.0
        seen = False
        while time.monotonic() < deadline:
            if log.exists() and b"FIRST LINE" in log.read_bytes():
                seen = True
                break
            if proc.poll() is not None:
                raise AssertionError(_explain("the driver exited before emitting anything"))
            time.sleep(0.05)

        if not seen:
            raise AssertionError(
                _explain(
                    "the first line never appeared while the child was running: "
                    "the stream is being held back, which is the #2145 regression"
                )
            )
        assert proc.poll() is None, _explain(
            "the child had already exited, so this proved nothing about streaming"
        )

        release.write_bytes(b"go")
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode(errors="replace")[-500:]
        assert b"LAST LINE" in log.read_bytes()
        assert self.KEY not in log.read_bytes()
