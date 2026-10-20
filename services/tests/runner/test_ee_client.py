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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from terrapod.runner import exec_subprocess
from terrapod.runner.ee_client import (
    ENDPOINT_ENV,
    TOKEN_ENV,
    EEResult,
    ExecutionEnvironmentUnavailable,
    cancel,
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

    def __init__(self, chunks, verdict, *, exec_status=200, trailer_rc=None):
        self.chunks = chunks
        self.verdict = verdict
        self.exec_status = exec_status
        self.trailer_rc = trailer_rc
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
                    for chunk in agent.chunks:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
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
        # The wire form is KEY=VALUE, matching os.Environ().
        assert sent["env"] == ["ANSIBLE_FORCE_COLOR=0"]

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
