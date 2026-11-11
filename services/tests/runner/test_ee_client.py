"""The orchestrator's side of the execution-environment contract (#1972).

Driven against a REAL HTTP server rather than a mocked `httpx`, because the
properties under test are all about the transport: that a streamed body arrives
incrementally, that the exit code is read from `/result` rather than from a
trailer no Python client can see, and that an unreachable agent fails closed.
Mocking the client would assert our own expectations back at us -- which is how
the trailer contract came to be verified only in the language that can read it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from terrapod.runner import exec_subprocess
from terrapod.runner.ee_client import (
    ENDPOINT_ENV,
    TOKEN_ENV,
    EEResult,
    ExecutionEnvironmentUnavailable,
    cancel,
    child_env,
    configured,
    run,
)

TOKEN = "agent-token"

#: Sentinel for "the fixture picks", so a caller can ask for no verdict at all.
_DEFAULT = object()


class _FakeAgent:
    """An agent that behaves like `ee-agent` on the wire.

    `verdict=None` serves 404 from /result, which is the agent that streamed and
    then could not say how it ended.
    """

    def __init__(
        self,
        chunks,
        verdict,
        *,
        exec_status=200,
        trailer_rc=None,
        chunk_delay=0.0,
        after_first_chunk=None,
    ):
        self.chunks = chunks
        self.verdict = verdict
        self.exec_status = exec_status
        self.trailer_rc = trailer_rc
        # Space the chunks out, so a test can act WHILE the request is in
        # flight. Without it the fake finishes in under a millisecond and a
        # signal fired "during" the run actually lands after it -- which is a
        # race in the test rather than in the code, and it passes or fails on
        # machine speed.
        self.chunk_delay = chunk_delay
        # Called once, after the FIRST chunk has been written and flushed.
        #
        # This is what lets a test act during the request without a timer. A
        # timer races the stream: the signal has to land after the client has
        # installed its handler and before the stream ends, so the test passes
        # on headroom rather than on ordering -- and a loaded CI runner starved
        # the timer thread past the end of a two-second stream, which read as
        # "a SIGTERM did not reach the execution environment" when the signal
        # had simply arrived late. A hook here is CAUSED by the stream, so it
        # cannot precede the handler, and a stream that keeps going until
        # `cancelled` cannot end before the signal is observed.
        self.after_first_chunk = after_first_chunk
        self.seen: list[dict] = []
        self.cancelled = False
        agent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # keep the test output clean
                pass

            def _auth_ok(self):
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def _empty(self, status):
                # Content-Length even on a bodyless reply: `protocol_version`
                # is HTTP/1.1, so the connection is keep-alive and an unframed
                # response leaves the client waiting for a body that never ends.
                # The real agent frames its refusals (Go's http.Error does), so
                # an unframed fake would be testing a server that cannot exist
                # -- and it hangs the suite rather than failing it.
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                if not self._auth_ok():
                    self._empty(403)
                    return
                if self.path == "/exec":
                    length = int(self.headers.get("Content-Length", 0))
                    agent.seen.append(json.loads(self.rfile.read(length) or b"{}"))
                    if agent.exec_status != 200:
                        self._empty(agent.exec_status)
                        return
                    self.send_response(agent.exec_status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    if agent.trailer_rc is not None:
                        self.send_header("Trailer", "X-Terrapod-RC")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for index, chunk in enumerate(agent.chunks):
                        # Stop when cancelled, as the real agent does: it SIGINTs
                        # ansible, which exits, and the stream ends. That also
                        # takes most of the timing out of the cancellation tests
                        # -- they finish as soon as the cancellation is observed
                        # rather than racing the end of a fixed-length stream.
                        if agent.cancelled:
                            break
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                        if index == 0 and agent.after_first_chunk is not None:
                            agent.after_first_chunk()
                        if agent.chunk_delay:
                            time.sleep(agent.chunk_delay)
                    # A trailer the client is expected to ignore entirely.
                    if agent.trailer_rc is not None:
                        self.wfile.write(b"0\r\nX-Terrapod-RC: %d\r\n\r\n" % agent.trailer_rc)
                    else:
                        self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
                if self.path == "/cancel":
                    agent.cancelled = True
                    body = json.dumps({"signalled": True}).encode()
                    self.send_response(202)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._empty(404)

            def do_GET(self):
                if self.path != "/result":
                    self._empty(404)
                    return
                if not self._auth_ok():
                    self._empty(403)
                    return
                if agent.verdict is None:
                    self._empty(404)
                    return
                body = json.dumps(agent.verdict).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def agent(monkeypatch):
    made: list[_FakeAgent] = []

    def make(chunks=(b"PLAY [x] ***\n", b"ok: [host1]\n"), verdict=_DEFAULT, **kw):
        # A distinct sentinel, because `None` is MEANINGFUL here -- it is the
        # agent that streamed and then could not say how it ended. Defaulting on
        # `None` made that case silently serve rc=0 instead, so the test for it
        # passed vacuously in the one direction that matters.
        if verdict is _DEFAULT:
            verdict = {"bin": "ansible-playbook", "rc": 0}
        a = _FakeAgent(list(chunks), verdict, **kw)
        made.append(a)
        monkeypatch.setenv(ENDPOINT_ENV, a.endpoint)
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
        return a

    yield make
    for a in made:
        a.close()


@pytest.fixture
def absorb_sigterm():
    """Make SIGTERM harmless for the duration of a test, then restore it.

    The cancellation tests fire a real signal at this process, and the handler
    under test is what stops the default action from killing it. So WITHOUT this
    a regression that drops the handler does not fail those tests -- it kills
    pytest, exit 143, with no `FAILED` line and 16 of 19 tests reported. That
    reads as "nothing failed", which is the worst possible shape for a
    mutation check and makes the suite fragile besides.

    Installed BEFORE `ee_client` installs its own, so the context manager saves
    this as the handler to restore and the window under test is unchanged.
    """
    import signal as signalmod

    previous = signalmod.signal(signalmod.SIGTERM, lambda *_: None)
    try:
        yield
    finally:
        signalmod.signal(signalmod.SIGTERM, previous)


@pytest.fixture(autouse=True)
def _no_secrets():
    exec_subprocess._reset_secrets_for_tests()
    yield
    exec_subprocess._reset_secrets_for_tests()


class TestConfigured:
    def test_absent_env_means_this_pod_has_no_execution_environment(self, monkeypatch):
        monkeypatch.delenv(ENDPOINT_ENV, raising=False)
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        assert configured() is False

    def test_both_are_required_not_either(self, monkeypatch):
        monkeypatch.setenv(ENDPOINT_ENV, "http://127.0.0.1:1")
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        assert configured() is False

    def test_a_phase_run_without_one_refuses_naming_both(self, monkeypatch):
        monkeypatch.delenv(ENDPOINT_ENV, raising=False)
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        with pytest.raises(ExecutionEnvironmentUnavailable) as e:
            run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert ENDPOINT_ENV in str(e.value) and TOKEN_ENV in str(e.value)


class TestTheHappyPath:
    def test_the_body_reaches_the_log_and_the_rc_comes_from_result(self, agent, tmp_path):
        a = agent(verdict={"bin": "ansible-playbook", "rc": 0})
        logf = tmp_path / "configure.log"
        result = run(
            "ansible-playbook",
            ["site.yml", "--limit", "web"],
            cwd="/workspace/ansible",
            env={"ANSIBLE_FORCE_COLOR": "0"},
            log_file=str(logf),
            tee_to_stdout=False,
        )
        assert result == EEResult(bin="ansible-playbook", rc=0, error="")
        assert "PLAY [x] ***" in logf.read_text()
        assert "ok: [host1]" in logf.read_text()

        sent = a.seen[0]
        assert sent["bin"] == "ansible-playbook"
        assert sent["args"] == ["site.yml", "--limit", "web"]
        assert sent["dir"] == "/workspace/ansible"
        # The wire form is KEY=VALUE, matching os.Environ(). `PYTHONUNBUFFERED`
        # is forced onto every child (#2511) and so is part of this contract;
        # asserting the set rather than the order, which nothing depends on.
        assert set(sent["env"]) == {"ANSIBLE_FORCE_COLOR=0", "PYTHONUNBUFFERED=1"}

    def test_the_log_is_appended_not_truncated(self, agent, tmp_path):
        """A configure is concatenated onto the apply's log (#1988).

        Truncating would delete the apply the operator is watching.
        """
        agent()
        logf = tmp_path / "combined.log"
        logf.write_text("...the apply that came first\n")
        run("ansible-playbook", [], cwd="/w", log_file=str(logf), tee_to_stdout=False)
        body = logf.read_text()
        assert "the apply that came first" in body
        assert "PLAY [x] ***" in body


class TestTheExitCodeDoesNotComeFromTheTrailer:
    def test_a_contradicting_trailer_is_ignored(self, agent, tmp_path):
        """The trailer is unreadable from Python, so it must not be consulted.

        The fake serves rc=0 in a trailer and rc=2 from /result. A client that
        somehow read the trailer would report success on a failed configure --
        the dangerous direction -- so the assertion is that /result wins.
        """
        agent(verdict={"bin": "ansible-playbook", "rc": 2}, trailer_rc=0)
        result = run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert result.rc == 2

    def test_some_hosts_failed_is_ansibles_verdict_not_an_agent_error(self, agent):
        agent(verdict={"bin": "ansible-playbook", "rc": 2})
        result = run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert result.rc == 2
        assert result.error == ""

    def test_an_agent_refusal_is_distinguishable_from_a_failed_playbook(self, agent):
        """rc=126 with a reason: the EE cannot run playbooks at all.

        Without this an operator cannot tell a broken estate from a broken
        execution environment.
        """
        agent(
            chunks=[],
            verdict={
                "bin": "ansible-playbook",
                "rc": 126,
                "error": '"ansible-playbook" is not an ansible entry point this agent will run',
            },
        )
        result = run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert result.rc == 126
        assert "not an ansible entry point" in result.error


class TestItFailsClosed:
    def test_an_unreachable_agent_raises_rather_than_reporting_success(self, monkeypatch):
        """rc=0 with an empty log reads as "changed nothing" to every caller."""
        monkeypatch.setenv(ENDPOINT_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
        with pytest.raises(ExecutionEnvironmentUnavailable) as e:
            run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert "127.0.0.1:1" in str(e.value)

    def test_a_stream_that_never_reports_a_verdict_raises(self, agent, tmp_path):
        """Ran, produced output, and /result 404s. The rc is unknown, not zero."""
        agent(verdict=None)
        logf = tmp_path / "c.log"
        with pytest.raises(ExecutionEnvironmentUnavailable) as e:
            run("ansible-playbook", [], cwd="/w", log_file=str(logf), tee_to_stdout=False)
        assert "no verdict" in str(e.value)
        # The output still reached the log -- a failure to learn the code must
        # not also lose what ansible said.
        assert "PLAY [x] ***" in logf.read_text()

    def test_a_refused_exec_raises(self, agent):
        agent(exec_status=403)
        with pytest.raises(ExecutionEnvironmentUnavailable) as e:
            run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert "403" in str(e.value)


class TestRedactionCrossesTheContainerBoundary:
    """#2154 scope item 11: ansible's stdout now originates in another container.

    The redactor must still cover it, or a machine credential (#1933) echoed by a
    playbook reaches the run log in clear.
    """

    def test_a_registered_secret_is_masked_in_the_streamed_body(self, agent, tmp_path):
        secret = "s3cret-machine-credential-value"
        assert exec_subprocess.register_secret(secret)
        agent(chunks=[f"ok: [host1] => password={secret}\n".encode()])
        logf = tmp_path / "c.log"
        run("ansible-playbook", [], cwd="/w", log_file=str(logf), tee_to_stdout=False)
        body = logf.read_text()
        assert secret not in body
        assert "ok: [host1]" in body

    def test_a_secret_split_across_two_chunks_is_still_masked(self, agent, tmp_path):
        """The straddle case, which a per-chunk replace misses silently."""
        secret = "straddling-secret-value-long-enough"
        assert exec_subprocess.register_secret(secret)
        head, tail = secret[:10], secret[10:]
        agent(chunks=[f"before {head}".encode(), f"{tail} after\n".encode()])
        logf = tmp_path / "c.log"
        run("ansible-playbook", [], cwd="/w", log_file=str(logf), tee_to_stdout=False)
        body = logf.read_text()
        assert secret not in body
        assert "before " in body and "after" in body


class TestCancel:
    def test_it_reports_whether_anything_was_signalled(self, agent):
        a = agent()
        assert cancel() is True
        assert a.cancelled is True

    def test_without_an_execution_environment_there_is_nothing_to_cancel(self, monkeypatch):
        monkeypatch.delenv(ENDPOINT_ENV, raising=False)
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        assert cancel() is False

    def test_an_unreachable_agent_is_not_an_error_to_cancel(self, monkeypatch):
        """Cancellation races a child that may already have finished."""
        monkeypatch.setenv(ENDPOINT_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(TOKEN_ENV, TOKEN)
        assert cancel() is False


class TestTerminationReachesTheExecutionEnvironment:
    """#2154 item 12: the runner's SIGTERM contract now spans two containers.

    `exec_subprocess.run` forwards a signal to the child it started. A configure
    has no local child -- ansible runs in the EE, parented by the agent -- so
    nothing else would carry the duty, and a terminating pod would leave ansible
    running until the kubelet killed the whole pod part way through a fleet.
    """

    def test_a_sigterm_during_a_run_cancels_in_the_ee(self, agent, absorb_sigterm):
        """Fired BY the stream rather than by a timer racing it.

        The timer version failed on a loaded CI runner: it fired the signal 50ms
        in against a two-second stream, and the timer thread was starved past
        the end of the stream, so the run completed normally and the assertion
        read as "a SIGTERM did not reach the execution environment" when the
        signal had simply arrived late. Widening the stream would only buy more
        headroom for the same race.

        Firing from the fake after its first chunk removes the ordering
        question instead: the signal cannot precede the handler, because the
        handler is installed before the request that produces the chunk. Python
        delivers a process-directed signal to the main thread, which is where
        the handler is, so this still exercises the real path. The chunk budget
        is the only deadline left and it bounds a genuine regression rather than
        this test -- the stream stops as soon as cancellation is observed, so a
        passing run ends in milliseconds.
        """
        import os
        import signal as signalmod

        a = agent(
            chunks=[b"PLAY [x] ***\n"] * 2000,
            chunk_delay=0.005,
            after_first_chunk=lambda: os.kill(os.getpid(), signalmod.SIGTERM),
        )
        run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert a.cancelled is True, "a SIGTERM did not reach the execution environment"

    def test_the_handler_is_restored_afterwards(self, agent, absorb_sigterm):
        """It affects exactly the window where there is something to cancel."""
        import signal as signalmod

        agent()
        before = signalmod.getsignal(signalmod.SIGTERM)
        run("ansible-playbook", [], cwd="/w", tee_to_stdout=False)
        assert signalmod.getsignal(signalmod.SIGTERM) is before

    def test_only_one_cancellation_is_sent(self, monkeypatch, absorb_sigterm):
        """The guard asserted directly, not through two racing signals.

        The signal-driven version of this test was VACUOUS and the mutation
        check is what showed it: the fake stops streaming when cancelled -- as
        the real agent does, because it SIGINTs ansible and the stream ends -- so
        a second signal fired at the process arrives after the request has
        finished and the handler has been restored. It passed whether or not the
        guard existed.

        Invoking the installed handler twice tests the property itself: the agent
        forwards ONE SIGINT, and a second makes ansible abort ungracefully,
        possibly skipping what it was part way through. Mirrors
        `exec_subprocess`'s own one-shot guard, for the same reason.
        """
        import signal as signalmod

        import terrapod.runner.ee_client as mod

        sent: list[int] = []
        monkeypatch.setattr(mod, "cancel", lambda: sent.append(1) or True)

        with mod._forward_termination_to_the_execution_environment("ansible-playbook"):
            handler = signalmod.getsignal(signalmod.SIGTERM)
            assert callable(handler), "no handler was installed to test"
            handler(signalmod.SIGTERM, None)
            handler(signalmod.SIGTERM, None)

        assert len(sent) == 1, f"cancelled {len(sent)} times"


class TestTheChildIsForcedUnbuffered:
    """#2511: a configure's log arrived in large irregular chunks.

    Everything else in this module already streams -- the agent flushes per
    write, httpx hands each chunk straight on, and the redactor holds nothing
    back for ordinary output; all three measured. None of it helps while the
    CHILD is sitting on its own bytes, which is what ansible does on a pipe: it
    never flushes, and CPython block-buffers a non-tty at 8 KiB.

    Measured against real ansible-core 2.21.5, three hosts sleeping 2/6/10s
    inside ONE task: without the variable all three `ok:` lines reached the
    reader at +10.87s, with it at +2.83s / +6.83s / +10.81s.
    """

    def test_it_is_forced_when_the_caller_passes_nothing(self):
        assert child_env(None) == {"PYTHONUNBUFFERED": "1"}
        assert child_env({}) == {"PYTHONUNBUFFERED": "1"}

    def test_the_callers_own_variables_are_kept(self):
        """Forcing one key must not become replacing the environment."""
        assert child_env({"ANSIBLE_FORKS": "20"}) == {
            "ANSIBLE_FORKS": "20",
            "PYTHONUNBUFFERED": "1",
        }

    def test_a_caller_cannot_turn_it_off(self):
        """Ours wins, and that direction is a judgement rather than an accident.

        A phase builds its environment for ansible's benefit and has no reason
        to think about the log's shape; `galaxy_env` is a strict allow-list and
        `pip_env` is assembled somewhere else again. A forced value that lost to
        any of them would restore #2511 for one binary and leave the rest
        streaming, which is the shape of defect nobody goes looking for.
        """
        assert child_env({"PYTHONUNBUFFERED": ""})["PYTHONUNBUFFERED"] == "1"
        assert child_env({"PYTHONUNBUFFERED": "0"})["PYTHONUNBUFFERED"] == "1"

    def test_the_call_site_puts_it_on_the_wire(self, agent):
        """The decision is inert unless `run` actually sends it.

        Separate from the cases above, which would all still pass with `run`
        building its payload from the caller's env directly -- which is exactly
        the line this fix changes.
        """
        a = agent()
        run("ansible-galaxy", ["collection", "install"], cwd="/w", tee_to_stdout=False)
        assert "PYTHONUNBUFFERED=1" in a.seen[0]["env"]

    def test_every_binary_gets_it_not_just_the_playbook(self, agent):
        """One door, because the agent's allowlist is all Python entry points.

        The report named the setup steps as well as the play, and
        `ansible-galaxy` is the worse case of the two: it forks nothing, so
        nothing incidentally flushes it and its whole progress stream waits for
        the process to exit.
        """
        a = agent()
        for binary in ("ansible-playbook", "ansible-galaxy", "pip", "ansible-inventory"):
            run(binary, [], cwd="/w", tee_to_stdout=False)
        assert [b["bin"] for b in a.seen if "PYTHONUNBUFFERED=1" not in b["env"]] == []

    def test_a_captured_call_is_forced_too(self, agent):
        """`capture=True` suppresses the log file and the tee, not the variable."""
        a = agent()
        run("ansible-config", ["dump"], cwd="/w", capture=True)
        assert "PYTHONUNBUFFERED=1" in a.seen[0]["env"]

    def test_what_run_sends_really_releases_an_unflushed_write(self):
        """The mechanism, driven rather than described -- and with no timing.

        The child writes a line and leaves through `os._exit`, which skips the
        interpreter's flush at shutdown. That stands in for the real case, where
        ansible has simply not reached a flush point yet because the task is
        still running, and it turns the question into "did the write reach the
        reader on its own" -- nothing to go flaky.

        The environment is `child_env` 's own output rather than a literal, so
        this cannot drift into proving a variable the product does not set; and
        it drives a real interpreter on a real pipe rather than asserting the
        string's shape, because the value configures CPython and a test that
        only checked our spelling would pass on a name that does nothing.
        """
        source = "import os, sys\nsys.stdout.write('STREAMED-WITHOUT-FLUSHING\\n')\nos._exit(0)\n"
        argv = [sys.executable, "-c", source]
        path = {"PATH": os.environ.get("PATH", "")}

        held = subprocess.run(argv, capture_output=True, env=path, check=False)
        assert held.stdout == b"", "the premise is gone: an unflushed write was already visible"

        forced = subprocess.run(
            argv, capture_output=True, env={**path, **child_env(None)}, check=False
        )
        assert b"STREAMED-WITHOUT-FLUSHING" in forced.stdout
