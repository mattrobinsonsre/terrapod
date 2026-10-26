"""Redacting a configure's STRUCTURED stream, and the forms a secret arrives in (#1934).

A configure has exactly two durable streams. The human one was already covered:
`ee_client` feeds the execution environment's stdout through `StreamRedactor` on
the way to the log file. The structured one was not covered at all -- the
callback writes `result._result` verbatim from inside the EE, where no secret of
ours exists or should -- and it is the stream that matters more, because
`host_results_service` lifts `payload.msg` into a database column served at
`run:read`, a tier BELOW the artifact itself.

**The finding that decides the design, and it is invisible unless you look at the
bytes.** A byte-wise pass over the raw secret matches NOTHING in that file, for
exactly the secrets worth protecting. Measured, with the real callback producing
it and an SSH private key registered:

    the raw value appears in the file   : False   <- so the pass reports no work
    its JSON-escaped form appears       : True    <- `-----BEGIN ...-----\\nb3Blb...`
    after redacting, the key is visible : True

JSON escapes per character and is context-free, so `json.dumps(v)[1:-1]` is
exactly the form present. The same blindness is in the HUMAN log and reachable
through a setting Terrapod deliberately leaves to the operator: with their
`ansible.cfg` setting `result_format = json`, our own stdout callback prints the
escaped form too. So the forms are derived once, in `register_secret`, and both
streams gain them from one producer.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import textwrap

import pytest

from terrapod.runner import exec_subprocess
from terrapod.runner.phases import uploads

#: A private key is the case the raw-form pass cannot see, and it is not a
#: contrived one: `private_key` is a registered secret field (#1933) and a
#: multi-line value is what a key is.
KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtz\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)

#: A password carrying a quote. Shorter path to the same property, and the one an
#: operator is more likely to have.
QUOTED = 'p@ss"word-long-enough-to-register'

#: Its own escaped form, so a deployment running only terraform or tofu pays
#: nothing: this is the shape of the cloud identity token, the only value
#: registered on that path.
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ3cy0xIn0.c2lnbmF0dXJlLWJ5dGVzLWhlcmU"


@pytest.fixture(autouse=True)
def _no_secrets_across_tests():
    exec_subprocess._reset_secrets_for_tests()
    yield
    exec_subprocess._reset_secrets_for_tests()


def _jsonl(*payloads: dict) -> bytes:
    """A stream in the callback's own encoding, so the escaping is not invented.

    `separators` and the default `ensure_ascii` match
    `terrapod_events.CallbackModule._write`, which is what makes a form derived
    here the form that is in the real file.
    """
    return b"".join(
        json.dumps(p, default=str, separators=(",", ":")).encode() + b"\n" for p in payloads
    )


class TestTheFormsAValueCanAppearIn:
    def test_a_multiline_secret_registers_its_escaped_forms(self):
        """The defect in one assertion: the raw form is not what is in the file."""
        assert exec_subprocess.register_secret(KEY) is True
        escaped = json.dumps(KEY)[1:-1].encode()
        assert escaped in exec_subprocess._SECRETS, (
            "only the raw form was registered, so a secret inside any JSON string "
            "passes the redactor untouched"
        )
        assert KEY.encode() in exec_subprocess._SECRETS, "the raw form is still needed"

    def test_a_quoted_password_registers_its_escaped_form(self):
        exec_subprocess.register_secret(QUOTED)
        assert json.dumps(QUOTED)[1:-1].encode() in exec_subprocess._SECRETS

    def test_a_value_with_nothing_to_escape_registers_exactly_one_form(self):
        """Why terraform and tofu pay nothing for this.

        A JWT is base64url and dots, and an ordinary password has no escapable
        character either, so each is its own encoding and the set is byte-identical
        to what it held before this existed. The requirement is #1934's own:
        a deployment running only terraform must pay nothing.
        """
        exec_subprocess.register_secret(JWT)
        assert exec_subprocess._SECRETS == {JWT.encode()}

    def test_the_depth_is_bounded_and_covers_json_inside_json(self):
        """Two levels, because the second is a real shape rather than a tidy one.

        A `uri` task reading a key out of a secrets API gets a JSON body whose own
        encoding escaped the newlines once; the callback then encodes that string
        again. Beyond two the nesting has to be JSON in JSON in JSON, which is the
        residual `docs/ansible-credentials.md` names rather than paying for here.
        """
        forms = exec_subprocess._match_forms(KEY)
        assert len(forms) == exec_subprocess._ESCAPE_DEPTH + 1
        assert len(set(forms)) == len(forms), "a repeated form means the loop over-ran"
        once = json.dumps(KEY)[1:-1]
        assert forms[1] == once.encode()
        assert forms[2] == json.dumps(once)[1:-1].encode()

    def test_the_forms_stop_as_soon_as_a_level_is_its_own_encoding(self):
        assert exec_subprocess._match_forms(JWT) == [JWT.encode()]


class TestTheFloorIsPerCaller:
    def test_the_default_floor_skips_a_short_value_and_registers_no_form_either(self):
        assert exec_subprocess.register_secret("short") is False
        assert exec_subprocess._SECRETS == set(), (
            "a value below the floor contributed a form, so the floor is not a floor"
        )

    def test_a_caller_whose_secret_has_no_mechanism_may_lower_it(self):
        """`password` and `become_password` are substituted into the inventory as
        VALUES, so the redactor is the only control over them -- unlike a private
        key or a vault password, which are 0600 files and protected by that. The
        default floor was chosen for a JWT's shape and says so."""
        assert exec_subprocess.register_secret("hunter22", min_length=8) is True
        assert b"hunter22" in exec_subprocess._SECRETS

    def test_the_lowered_floor_is_still_a_floor(self):
        assert exec_subprocess.register_secret("prod", min_length=8) is False
        assert exec_subprocess._SECRETS == set()

    def test_the_value_delivered_fields_are_the_ones_that_lowered_it(self):
        """Derived from the credential module's own sets rather than listed here,
        so a new value-delivered secret field is covered without an edit."""
        from terrapod.services.ansible_auth import (
            MACHINE_FILE_FIELDS,
            MACHINE_SECRET_FIELDS,
            MACHINE_VALUE_FIELDS,
        )

        lowered = MACHINE_SECRET_FIELDS & MACHINE_VALUE_FIELDS
        assert lowered == {"password", "become_password"}
        assert not (lowered & MACHINE_FILE_FIELDS), (
            "a field delivered as a FILE reached the lowered floor; the file is its "
            "control and the default floor is right for it"
        )


class TestRedactingAWholeDocument:
    def _stream(self, tmp_path: pathlib.Path, *payloads: dict) -> pathlib.Path:
        path = tmp_path / "events.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_jsonl(*payloads))
        return path

    def test_a_secret_escaped_inside_a_json_string_is_removed(self, tmp_path):
        """The measured defect, end to end through the function that fixes it."""
        exec_subprocess.register_secret(KEY)
        path = self._stream(tmp_path, {"kind": "ok", "payload": {"msg": f"ssh said: {KEY}"}})
        assert KEY.encode() not in path.read_bytes(), (
            "the fixture does not reproduce the defect: the raw form is present, so "
            "a raw-only pass would have worked and this proves nothing"
        )

        assert exec_subprocess.redact_file(path) is True
        out = path.read_bytes()
        assert b"OPENSSH PRIVATE KEY" not in out
        assert b"[redacted]" in out

    def test_the_stream_is_still_valid_json_per_line(self, tmp_path):
        """A reader parses this file; a redaction that broke the encoding would
        trade a leak for an unreadable artifact."""
        exec_subprocess.register_secret(KEY)
        exec_subprocess.register_secret(QUOTED)
        path = self._stream(
            tmp_path,
            {"kind": "ok", "payload": {"msg": f"key {KEY}"}},
            {"kind": "failed", "payload": {"msg": f"password {QUOTED}"}},
        )
        exec_subprocess.redact_file(path)
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert len(records) == 2
        assert all("[redacted]" in r["payload"]["msg"] for r in records)

    def test_the_raw_form_is_removed_too(self, tmp_path):
        """A secret with nothing to escape appears raw, which is the common case."""
        exec_subprocess.register_secret(JWT)
        path = self._stream(tmp_path, {"payload": {"stdout": f"token={JWT}"}})
        assert exec_subprocess.redact_file(path) is True
        assert JWT.encode() not in path.read_bytes()

    def test_a_clean_document_is_byte_identical_and_reported_safe(self, tmp_path):
        """The overwhelming case, and the one the return value must not confuse
        with a failed rewrite."""
        exec_subprocess.register_secret(KEY)
        path = self._stream(tmp_path, {"kind": "ok", "payload": {"msg": "nothing secret here"}})
        before = path.read_bytes()
        assert exec_subprocess.redact_file(path) is True
        assert path.read_bytes() == before

    def test_a_missing_file_is_safe_rather_than_a_refusal(self, tmp_path):
        assert exec_subprocess.redact_file(tmp_path / "never-written.jsonl") is True

    def test_it_keeps_the_owner_only_mode(self, tmp_path):
        """The callback writes 0600 and the replacement must not widen it -- the
        volume is shared with the execution environment."""
        exec_subprocess.register_secret(KEY)
        path = self._stream(tmp_path, {"payload": {"msg": KEY}})
        exec_subprocess.redact_file(path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_a_rewrite_it_cannot_do_is_reported_as_unsafe(self, tmp_path):
        """And the original is left intact rather than half-written.

        The refusal is made STRUCTURALLY, by putting a directory where the
        temporary file goes: `O_CREAT` on a directory is `EISDIR`, which the
        kernel refuses for every uid. A mode-based denial would prove nothing,
        because the containerised suite runs as root and `CAP_DAC_OVERRIDE`
        walks through one -- the recorded trap that made an earlier guard inert
        in the only place that gates a merge.
        """
        exec_subprocess.register_secret(KEY)
        path = self._stream(tmp_path, {"payload": {"msg": KEY}})
        (tmp_path / "events.jsonl.redacting").mkdir()

        assert exec_subprocess.redact_file(path) is False
        assert b"OPENSSH PRIVATE KEY" in path.read_bytes(), "the original was damaged"

    def test_nothing_registered_leaves_the_document_alone(self, tmp_path):
        path = self._stream(tmp_path, {"payload": {"msg": KEY}})
        before = path.read_bytes()
        assert exec_subprocess.redact_file(path) is True
        assert path.read_bytes() == before


class TestTheHumanLogGainsTheSameForms:
    """The other half of "one producer, both streams", and it is reachable.

    `ANSIBLE_CALLBACK_RESULT_FORMAT` is stripped from the inherited environment,
    but an operator's own `ansible.cfg` can set `result_format = json` and
    `configure.py` promises never to touch that file -- so our stdout callback
    renders results as JSON and the HUMAN log carries the escaped form too.
    Measured with our own renderer and their config:

        result_format = yaml  escaped form in the log: 0
        result_format = json  escaped form in the log: 1

    Driven through `exec_subprocess.run`, because that is the path the stream
    takes: a test over `_redact` alone would pass with the stream redactor
    bypassed.
    """

    def _emit(self, tmp_path: pathlib.Path, payload: str) -> str:
        helper = tmp_path / "p.py"
        helper.write_text(
            "import sys\nsys.stdout.write(open(sys.argv[1], encoding='utf-8').read())\n"
        )
        data = tmp_path / "payload.txt"
        data.write_text(payload, encoding="utf-8")
        log = tmp_path / "out.log"
        result = exec_subprocess.run(
            ["python3", str(helper), str(data)],
            log_file=str(log),
            tee_to_stdout=False,
        )
        assert result.exit_code == 0
        return log.read_text(encoding="utf-8", errors="replace")

    def test_an_escaped_secret_does_not_reach_the_run_log(self, tmp_path):
        exec_subprocess.register_secret(KEY)
        escaped = json.dumps(KEY)[1:-1]
        out = self._emit(tmp_path, f'ok: [web1] => {{"msg": "{escaped}"}}\n')
        assert "OPENSSH PRIVATE KEY" not in out
        assert "[redacted]" in out

    def test_the_raw_form_still_does_not_either(self, tmp_path):
        """`result_format = yaml` prints a multi-line value as a block, so the raw
        form is what appears there -- both have to hold at once."""
        exec_subprocess.register_secret(KEY)
        out = self._emit(tmp_path, f"ok: [web1] =>\n  msg: |-\n{KEY}\n")
        assert "OPENSSH PRIVATE KEY" not in out

    def test_an_unregistered_run_is_still_byte_identical(self, tmp_path):
        """What makes this cost a terraform-only deployment nothing: with nothing
        registered the bytes go through unexamined."""
        payload = f'ok: [web1] => {{"msg": "{json.dumps(KEY)[1:-1]}"}}\n'
        assert self._emit(tmp_path, payload) == payload


class TestNothingUploadsAnUnredactedCapture:
    """Asserted through `upload_host_events` itself, not through a helper.

    The thing being pinned is that the pass runs BEFORE the bytes leave, so a
    test that calls `redact_file` by hand would pass with the ordering reversed
    -- which is the recorded weak-test shape, and the whole reason the pass lives
    inside the uploader rather than beside it.
    """

    def _cfg(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            api_url="https://terrapod.test",
            run_id="run-1",
            auth_token="t",
            has_api=True,
        )

    def _capture(self, monkeypatch) -> list[tuple[str, bytes]]:
        sent: list[tuple[str, bytes]] = []

        def fake_put(cfg, url, path, *, content_type=None, client=None, **_):  # noqa: ARG001
            sent.append((url, pathlib.Path(path).read_bytes()))
            return True, 200

        monkeypatch.setattr(uploads, "_put_file", fake_put)
        return sent

    def _events(self, tmp_path, monkeypatch, *, spill: bool):
        from terrapod.runner.phases import host_events

        events = tmp_path / "events.jsonl"
        events.write_bytes(_jsonl({"kind": "ok", "payload": {"msg": f"ssh said: {KEY}"}}))
        large = tmp_path / "events-large.jsonl"
        if spill:
            large.write_bytes(_jsonl({"ref": 1, "payload": {"msg": f"ssh said: {KEY}"}}))
        monkeypatch.setattr(host_events, "events_path", lambda: events)
        monkeypatch.setattr(host_events, "large_path", lambda: large)
        return events, large

    def test_the_bytes_that_leave_carry_no_secret(self, tmp_path, monkeypatch):
        exec_subprocess.register_secret(KEY)
        sent = self._capture(monkeypatch)
        self._events(tmp_path, monkeypatch, spill=False)

        assert uploads.upload_host_events(self._cfg()) is True
        assert sent, "nothing was uploaded, so this asserts nothing"
        for url, body in sent:
            assert b"OPENSSH PRIVATE KEY" not in body, url
            assert b"[redacted]" in body, url

    def test_the_sidecar_is_redacted_too(self, tmp_path, monkeypatch):
        """Both objects or neither: the sidecar holds the payloads too large for
        the stream, which is where a `command`'s whole stdout ends up."""
        exec_subprocess.register_secret(KEY)
        sent = self._capture(monkeypatch)
        self._events(tmp_path, monkeypatch, spill=True)

        uploads.upload_host_events(self._cfg())
        assert len(sent) == 2, [u for u, _ in sent]
        assert any("host-events-large" in url for url, _ in sent)
        for url, body in sent:
            assert b"OPENSSH PRIVATE KEY" not in body, url

    def test_a_capture_it_could_not_redact_is_not_uploaded_at_all(self, tmp_path, monkeypatch):
        exec_subprocess.register_secret(KEY)
        sent = self._capture(monkeypatch)
        events, _ = self._events(tmp_path, monkeypatch, spill=False)
        (events.parent / "events.jsonl.redacting").mkdir()

        assert uploads.upload_host_events(self._cfg()) is False
        assert sent == [], "a secret that could not be removed was sent anyway"

    def test_a_sidecar_it_could_not_redact_does_not_stop_the_stream(self, tmp_path, monkeypatch):
        """The stream is already clean and already sent; refusing it too would
        discard evidence for no gain."""
        exec_subprocess.register_secret(KEY)
        sent = self._capture(monkeypatch)
        _, large = self._events(tmp_path, monkeypatch, spill=True)
        (large.parent / "events-large.jsonl.redacting").mkdir()

        uploads.upload_host_events(self._cfg())
        assert [url for url, _ in sent] == [
            "https://terrapod.test/api/terrapod/v1/runs/run-1/artifacts/host-events"
        ]


# --- driven through real ansible ------------------------------------------


def _working_ansible() -> str | None:
    """A usable `ansible-playbook`, or None. See `test_host_events_callback`."""
    found = shutil.which("ansible-playbook")
    if found is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603
            [found, "--version"], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if probe.returncode == 0 else None


ANSIBLE = _working_ansible()
needs_ansible = pytest.mark.skipif(ANSIBLE is None, reason="no working ansible-playbook")

_EVENTS_PLUGIN = (
    pathlib.Path(__file__).resolve().parents[2]
    / "terrapod"
    / "runner"
    / "ansible_callback"
    / "terrapod_events.py"
)


def _drive(root: pathlib.Path, play: str) -> pathlib.Path:
    """Run the play with the real callback; return the events file it wrote.

    HOME is NOT relocated, for the reason `test_host_events_callback` records: it
    moves Python's user site-packages with it, so a user-site ansible stops
    importing and the script dies before parsing its arguments.
    """
    plugins = root / "cb"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_EVENTS_PLUGIN, plugins / _EVENTS_PLUGIN.name)
    (root / "inventory.yml").write_text("all:\n  hosts:\n    web1: {ansible_connection: local}\n")
    (root / "site.yml").write_text(play)
    events = root / "events.jsonl"

    env = dict(os.environ)
    env.update(
        {
            "TP_HOST_EVENTS_PATH": str(events),
            "ANSIBLE_CALLBACK_PLUGINS": str(plugins),
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
        }
    )
    subprocess.run(  # noqa: S603
        [str(ANSIBLE), "-i", "inventory.yml", "site.yml"],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=300,
        check=False,
    )
    return events


def _records(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@needs_ansible
class TestWhatRealAnsiblePutsInTheStream:
    def test_a_resolved_credential_a_task_printed_does_not_survive_the_pass(self, tmp_path):
        """#1934's acceptance, against the real callback and real ansible.

        The fixture asserts the defect is reproduced before it asserts the fix,
        because the interesting half is that the raw value is absent from the
        file: a pass over the raw form alone finds nothing to do and reports
        success.
        """
        events = _drive(
            tmp_path,
            textwrap.dedent(
                """
                - hosts: all
                  gather_facts: false
                  vars:
                    delivered: |
                      -----BEGIN OPENSSH PRIVATE KEY-----
                      b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtz
                      -----END OPENSSH PRIVATE KEY-----
                  tasks:
                    - name: a module returns it in its result
                      ansible.builtin.debug:
                        msg: "ssh refused: {{ delivered }}"
                """
            ).strip(),
        )
        raw = events.read_bytes()
        assert b"OPENSSH PRIVATE KEY" in raw, "ansible did not report the task"

        # Registered exactly as the credential phase holds it: ansible's block
        # scalar yields the key with real newlines and a trailing one.
        material = next(
            r["payload"]["msg"].split("ssh refused: ", 1)[1]
            for r in _records(events)
            if r.get("kind") == "ok"
        )
        assert material.encode() not in raw, (
            "the raw form IS in the file, so this fixture no longer reproduces the "
            "defect and the assertion below would pass without the escaped forms"
        )
        assert exec_subprocess.register_secret(material) is True

        assert exec_subprocess.redact_file(events) is True
        assert b"OPENSSH PRIVATE KEY" not in events.read_bytes()
        assert all("[redacted]" in json.dumps(r) for r in _records(events) if r.get("kind") == "ok")

    def test_ansibles_own_no_log_censorship_reaches_the_stream(self, tmp_path):
        """Requirement 2: honour ansible's markers, do not re-derive them.

        Nothing is registered here on purpose. `TaskQueueManager.send_callback`
        hands a notification callback a `clean_copy()`, so a `no_log` result
        arrives already replaced by a `censored` marker -- measured on 2.18.3,
        and the property this pins is that the callback does not reach past it.
        The marker's TEXT is not asserted: it has been reworded upstream, and the
        claim is about the value being gone rather than about the sentence.

        This one cannot be mutation-checked from here, because there is nothing
        of ours to remove -- `result._result` IS the cleaned copy. What
        establishes it is not vacuous is its sibling above: the same fixture
        shape, without `no_log`, puts the value in the file.
        """
        events = _drive(
            tmp_path,
            textwrap.dedent(
                """
                - hosts: all
                  gather_facts: false
                  tasks:
                    - name: a task the author marked no_log
                      ansible.builtin.debug:
                        msg: "the value is SUPERSECRETVALUE0123456789abcdef"
                      no_log: true
                """
            ).strip(),
        )
        assert b"SUPERSECRETVALUE" not in events.read_bytes(), (
            "the callback reported a no_log result's content, so it is reaching past "
            "ansible's own censorship rather than honouring it"
        )
        results = [r for r in _records(events) if r.get("kind") == "ok"]
        assert results, "the task was not reported at all"
        assert "censored" in results[0]["payload"], results[0]["payload"]

    def test_a_modules_own_no_log_parameter_is_censored_before_we_see_it(self, tmp_path):
        """The other half of ansible's censorship, and it reaches `invocation`.

        **Asserted as the PROPERTY, because the mechanism differs by line and
        the first version of this test failed on CI's own.** Measured over four
        lines with this exact fixture:

            2.18.3  invocation=2  placeholder=1  secret=0
            2.19.6  invocation=2  placeholder=1  secret=0
            2.20.4  invocation=2  placeholder=1  secret=0
            2.21.5  invocation=0  placeholder=0  secret=0

        So up to 2.20 the module's own `no_log` replaces the value inside
        `invocation.module_args`, and on **2.21 `invocation` does not reach a
        callback at all, at any verbosity**. The value is absent either way,
        which is what matters; asserting the placeholder unconditionally pins
        2.20's mechanism and fails on the line the EE default actually runs.

        `invocation` is not stripped by us where ansible provides it (user's
        decision, 2026-10-26): it answers "what did we actually run on this
        host", the stream is gated at `state:read` for exactly that content, and
        removing it would re-derive a censorship decision ansible's own renderer
        makes for noise rather than for secrecy.
        """
        events = _drive(
            tmp_path,
            textwrap.dedent(
                """
                - hosts: all
                  gather_facts: false
                  tasks:
                    - name: a module parameter the module itself marks no_log
                      ansible.builtin.uri:
                        url: http://127.0.0.1:1/
                        url_username: bob
                        url_password: SUPERSECRETVALUE0123456789abcdef
                      ignore_errors: true
                """
            ).strip(),
        )
        body = events.read_bytes()
        results = [r for r in _records(events) if r.get("kind") in {"ok", "failed"}]
        assert results, "the task was not reported at all, so this asserts nothing"
        assert b"SUPERSECRETVALUE" not in body, (
            "the parameter reached the stream in the clear, so ansible's own "
            "censorship is not what is protecting it"
        )
        if b"invocation" in body:
            # The line still provides it; then the placeholder is the mechanism
            # and must be there, or the value was dropped rather than censored
            # and some other no_log parameter could survive.
            assert b"VALUE_SPECIFIED_IN_NO_LOG_PARAMETER" in body
