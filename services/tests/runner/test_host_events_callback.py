"""The event callback, driven through real ansible (#1928).

The plugin's output is consumed by an external tool -- ansible loads it and
calls its hooks -- so a shape assertion about the file it writes proves nothing
about whether ansible ever calls it. These tests run `ansible-playbook`.

Three of them pin properties that no amount of reading the plugin establishes,
and each was measured before it was asserted:

* a NOTIFICATION callback coexists with ansible's own stdout renderer, which is
  what lets #2144's stdout callback own the human log while this one owns the
  events;
* an operator's `callback_plugins` REPLACES the default search path, so setting
  `ANSIBLE_CALLBACK_PLUGINS` to our directory alone silently disables every
  notification callback they run -- the regression `host_events.callback_env`
  exists to avoid;
* `v2_runner_on_unreachable` fires while the recap reports `unreachable=0`
  once `ignore_unreachable` is set, which is why `saw_unreachable` is taken from
  the event.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import textwrap

import pytest

_PLUGIN = (
    pathlib.Path(__file__).resolve().parents[2]
    / "terrapod"
    / "runner"
    / "ansible_callback"
    / "terrapod_events.py"
)


def _working_ansible() -> str | None:
    """A usable `ansible-playbook`, or None.

    Probed rather than located, for the reason `test_inventory_resolve` records:
    a stale console script whose `ansible` module is gone dies with
    `ModuleNotFoundError`, which reads as a failure of the code under test.
    Skipping is right here and nowhere else -- the test image's own install is
    gated at BUILD time, so a broken install in CI fails the image build.
    """
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

#: The unreachable test needs the ssh TRANSPORT, not just ansible, and the
#: failure without it is measured rather than assumed. Driving this exact
#: fixture with ssh hidden from PATH:
#:
#:     with ssh     rc=0  unreachable=['hdead']  failed=[]
#:     without ssh  rc=2  unreachable=[]         failed=['hdead']
#:
#: So ansible reports the host FAILED, not unreachable -- `ignore_unreachable`
#: therefore does not apply either -- and the test fails on an empty list rather
#: than on the property it pins. That is how it failed in CI twice: the first fix
#: addressed the fixture's dependence on the network and not its missing binary.
#:
#: Skipping is right on a developer's machine and must NOT be how it behaves in
#: CI, so `docker/Dockerfile.test` installs `openssh-client` and asserts `ssh -V`
#: at BUILD time: a missing transport fails the image build, where it belongs,
#: rather than quietly skipping the one test that needs it.
needs_ssh = pytest.mark.skipif(
    shutil.which("ssh") is None,
    reason="no ssh transport, so ansible cannot report a host unreachable",
)


def _fixture(root: pathlib.Path, *, play: str, cfg: str | None = None) -> None:
    (root / "inventory.yml").write_text(
        textwrap.dedent(
            """
            all:
              children:
                web:
                  hosts:
                    web1: {ansible_connection: local}
                    web2: {ansible_connection: local}
                dead:
                  hosts:
                    hdead:
                      # Loopback with a port nothing can be listening on.
                      # ECONNREFUSED is immediate and STRUCTURAL -- no network,
                      # no DNS, no timeout -- so it means the same thing in a
                      # container with no egress. A reserved TEST-NET address
                      # over SSH does not: it fired the unreachable hook locally
                      # and fired NOTHING in CI, where this test then failed on
                      # an empty event list rather than on the property it
                      # exists to pin. Measured at 0.35s against a multi-second
                      # connect timeout.
                      ansible_host: 127.0.0.1
                      ansible_port: 1
                      ansible_connection: ssh
                      ansible_user: nobody
            """
        ).strip()
    )
    (root / "site.yml").write_text(play)
    if cfg is not None:
        (root / "ansible.cfg").write_text(cfg)


def _run(root: pathlib.Path, events: pathlib.Path, plugin_dirs: list[str]) -> int:
    """Drive ansible with the real plugin. HOME is NOT relocated.

    Relocating it moves Python's user site-packages with it, so a user-site
    ansible install stops importing and the console script dies before parsing
    its arguments -- a failure that names nothing about the plugin.
    """
    env = dict(os.environ)
    env.update(
        {
            "TP_HOST_EVENTS_PATH": str(events),
            "ANSIBLE_CALLBACK_PLUGINS": ":".join(plugin_dirs),
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_TIMEOUT": "5",
        }
    )
    completed = subprocess.run(  # noqa: S603
        [str(ANSIBLE), "-i", "inventory.yml", "site.yml"],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=300,
        check=False,
    )
    return completed.returncode


def _events(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@needs_ansible
class TestItCapturesWhatAnsibleReports:
    def test_a_notification_callback_coexists_with_ansibles_own_stdout(self, tmp_path):
        """The property #2144 depends on: we do not own stdout."""
        plugins = tmp_path / "cb"
        plugins.mkdir()
        shutil.copyfile(_PLUGIN, plugins / "terrapod_events.py")
        root = tmp_path / "play"
        root.mkdir()
        _fixture(
            root,
            play=textwrap.dedent(
                """
                - hosts: web
                  gather_facts: false
                  tasks:
                    - name: say hello
                      ansible.builtin.debug: {msg: "hello {{ inventory_hostname }}"}
                """
            ).strip(),
        )
        events = tmp_path / "events.jsonl"
        assert _run(root, events, [str(plugins)]) == 0
        records = _events(events)
        kinds = {r["kind"] for r in records}
        assert {"playbook_start", "play_start", "task_start", "ok", "stats"} <= kinds
        hosts = {r["host"] for r in records if "host" in r}
        assert hosts == {"web1", "web2"}

    def test_the_recap_reports_every_host_ansible_heard_about(self, tmp_path):
        plugins = tmp_path / "cb"
        plugins.mkdir()
        shutil.copyfile(_PLUGIN, plugins / "terrapod_events.py")
        root = tmp_path / "play"
        root.mkdir()
        _fixture(
            root,
            play=textwrap.dedent(
                """
                - hosts: web
                  gather_facts: false
                  tasks:
                    - name: fail on web2
                      ansible.builtin.command: /bin/false
                      when: inventory_hostname == 'web2'
                      ignore_errors: true
                """
            ).strip(),
        )
        events = tmp_path / "events.jsonl"
        _run(root, events, [str(plugins)])
        stats = [r for r in _events(events) if r["kind"] == "stats"]
        assert len(stats) == 1
        assert set(stats[0]["recap"]) == {"web1", "web2"}
        failed = [r for r in _events(events) if r["kind"] == "failed"]
        assert [(r["host"], r["ignored"]) for r in failed] == [("web2", True)]

    @needs_ssh
    def test_unreachable_fires_while_the_recap_says_zero(self, tmp_path):
        """The measured disagreement `saw_unreachable` exists for.

        With `ignore_unreachable: true` the summary reads `unreachable=0
        ignored=1` while the unreachable hook fires, so a reader taking the
        counters at face value calls a dead host a healthy one.
        """
        plugins = tmp_path / "cb"
        plugins.mkdir()
        shutil.copyfile(_PLUGIN, plugins / "terrapod_events.py")
        root = tmp_path / "play"
        root.mkdir()
        _fixture(
            root,
            play=textwrap.dedent(
                """
                - hosts: dead
                  gather_facts: false
                  tasks:
                    - name: debug needs no connection
                      ansible.builtin.debug: {msg: "nothing attempted"}
                    - name: ping DOES
                      ansible.builtin.ping:
                      ignore_unreachable: true
                """
            ).strip(),
        )
        events = tmp_path / "events.jsonl"
        _run(root, events, [str(plugins)])
        records = _events(events)
        unreachable = [r for r in records if r["kind"] == "unreachable"]
        assert [r["host"] for r in unreachable] == ["hdead"]
        recap = next(r for r in records if r["kind"] == "stats")["recap"]["hdead"]
        assert recap["unreachable"] == 0, (
            "the premise of saw_unreachable changed: ansible now counts an ignored "
            "unreachable in the recap, so the event is no longer the only source"
        )
        assert recap["ignored"] == 1

    def test_an_oversized_payload_goes_to_the_sidecar_and_is_recoverable(self, tmp_path):
        plugins = tmp_path / "cb"
        plugins.mkdir()
        shutil.copyfile(_PLUGIN, plugins / "terrapod_events.py")
        root = tmp_path / "play"
        root.mkdir()
        _fixture(
            root,
            play=textwrap.dedent(
                """
                - hosts: web
                  gather_facts: false
                  tasks:
                    - name: an oversized payload
                      ansible.builtin.command: python3 -c "print('x'*70000)"
                      changed_when: false
                """
            ).strip(),
        )
        events = tmp_path / "events.jsonl"
        assert _run(root, events, [str(plugins)]) == 0
        records = _events(events)
        spilled = [r for r in records if "payload_ref" in r]
        assert len(spilled) == 2, "both hosts' results exceed the spill threshold"
        assert all("payload" not in r for r in spilled)

        sidecar = tmp_path / "events-large.jsonl"
        assert sidecar.exists()
        larges = {r["ref"]: r["payload"] for r in _events(sidecar)}
        assert {r["payload_ref"] for r in spilled} == set(larges)
        assert all(len(p.get("stdout", "")) == 70000 for p in larges.values())
        # The whole point of the spill: the file a host filter scans stays small.
        assert events.stat().st_size < sidecar.stat().st_size

    def test_both_files_are_owner_only(self, tmp_path):
        """Owner-only, because the EE runs as the orchestrator's own uid.

        `_ee_security_context` pins `runAsUser` to 1000 (#1928), so the reader of
        this file shares our uid and the group bit would grant more than the
        contract needs. It was 0640 while that uid was unpinned.

        Set explicitly rather than left to the umask, which belongs to the EE
        image: a default 022 would create this 0644 -- world-readable.
        """
        plugins = tmp_path / "cb"
        plugins.mkdir()
        shutil.copyfile(_PLUGIN, plugins / "terrapod_events.py")
        root = tmp_path / "play"
        root.mkdir()
        _fixture(
            root,
            play=textwrap.dedent(
                """
                - hosts: web
                  gather_facts: false
                  tasks:
                    - name: t
                      ansible.builtin.debug: {msg: hi}
                """
            ).strip(),
        )
        events = tmp_path / "events.jsonl"
        _run(root, events, [str(plugins)])
        assert events.stat().st_mode & 0o777 == 0o600


@needs_ansible
class TestItDoesNotDisturbTheOperatorsOwnCallbacks:
    """The regression `callback_env`'s prepend exists to prevent.

    Measured: an operator's `callback_plugins` REPLACES the default search path,
    so pointing `ANSIBLE_CALLBACK_PLUGINS` at our directory alone leaves their
    own notification callbacks unable to load -- silently, with their
    `callbacks_enabled` still naming one.
    """

    @staticmethod
    def _theirs(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        theirs = tmp_path / "theirs"
        theirs.mkdir()
        marker = tmp_path / "operator-ran.txt"
        (theirs / "op_marker.py").write_text(
            textwrap.dedent(
                f'''
                from ansible.plugins.callback import CallbackBase
                DOCUMENTATION = """
                    name: op_marker
                    type: notification
                    short_description: the operator's own
                    description: the operator's own
                """
                class CallbackModule(CallbackBase):
                    CALLBACK_VERSION = 2.0
                    CALLBACK_TYPE = "notification"
                    CALLBACK_NAME = "op_marker"
                    CALLBACK_NEEDS_ENABLED = True
                    def v2_playbook_on_stats(self, stats):
                        open({str(marker)!r}, "a").write("ran\\n")
                '''
            ).strip()
        )
        return theirs, marker

    def _play(self, tmp_path):
        plugins = tmp_path / "cb"
        plugins.mkdir()
        shutil.copyfile(_PLUGIN, plugins / "terrapod_events.py")
        theirs, marker = self._theirs(tmp_path)
        root = tmp_path / "play"
        root.mkdir()
        _fixture(
            root,
            play=textwrap.dedent(
                """
                - hosts: web
                  gather_facts: false
                  tasks:
                    - name: t
                      ansible.builtin.debug: {msg: hi}
                """
            ).strip(),
            cfg=f"[defaults]\ncallback_plugins = {theirs}\ncallbacks_enabled = op_marker\n",
        )
        return plugins, theirs, root, marker

    def test_prepending_to_their_path_runs_both(self, tmp_path):
        plugins, theirs, root, marker = self._play(tmp_path)
        events = tmp_path / "events.jsonl"
        _run(root, events, [str(plugins), str(theirs)])
        assert events.exists() and events.stat().st_size > 0, "ours did not capture"
        assert marker.exists(), "the operator's own callback did not run"

    def test_ours_alone_captures_but_silently_disables_theirs(self, tmp_path):
        """The contrast, which is the whole justification for the round trip.

        If this ever starts passing with the marker present, ansible has changed
        how `callback_plugins` composes and `callback_env` can stop asking.
        """
        plugins, _theirs, root, marker = self._play(tmp_path)
        events = tmp_path / "events.jsonl"
        _run(root, events, [str(plugins)])
        assert events.exists() and events.stat().st_size > 0
        assert not marker.exists(), (
            "ansible now merges callback_plugins with the configured path, so the "
            "`ansible-config dump` round trip in host_events.callback_env is no "
            "longer needed -- re-measure before simplifying it"
        )
