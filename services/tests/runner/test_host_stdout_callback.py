"""The stdout callback, driven through real ansible (#2144).

The plugin REPLACES ansible's renderer, so every property worth asserting is a
property of what ansible actually prints through it. A shape assertion about the
class establishes none of them, and the first probe run proved why: the plugin
loaded, ran, and emitted eight `[WARNING]: Failure using method (...) in callback
plugin` lines and no task output at all, because a subclass with no
`DOCUMENTATION` inherits `default`'s code and not its OPTIONS. Nothing short of
running it would have shown that.

Four of these pin properties no text parser over ansible's combined stdout can
have, which is the whole argument for a callback rather than a parser:

* a multi-line result's CONTINUATION lines carry their host -- measured, only
  12 of 41 non-blank lines carry `[host]` in ansible's own output;
* `...ignoring` after an ignored failure carries its host, where the text form
  names none;
* task banners and the PLAY RECAP carry an explicit host-less MARKER, so a
  filtered view can show them as context and a viewer can still tell a tagged
  log from an untagged one;
* stripping the tag reproduces ansible's own bytes exactly, which is what makes
  "keep a traditional view" ansible's rendering rather than a reimplementation.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest

_PLUGIN = (
    pathlib.Path(__file__).resolve().parents[2]
    / "terrapod"
    / "runner"
    / "ansible_callback"
    / "terrapod_stdout.py"
)

#: The format's three literals, read from the plugin rather than restated, so a
#: change to either side cannot pass this file silently.
_SRC = _PLUGIN.read_text()
HEADER = re.search(r'^HEADER = "(?P<v>[^"]+)"', _SRC, re.M).group("v")
DELIMITER = re.search(r'^DELIMITER = "(?P<v>[^"]+)"', _SRC, re.M).group("v")
HOSTLESS = re.search(r'^HOSTLESS = "(?P<v>[^"]+)"', _SRC, re.M).group("v")


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

_PLAY = textwrap.dedent(
    """
    - name: tagging
      hosts: all
      gather_facts: false
      tasks:
        - name: a multi-line result
          ansible.builtin.debug:
            msg: "hello from {{ inventory_hostname }}"
        - name: skip on db1
          ansible.builtin.debug: {msg: "web only"}
          when: inventory_hostname in groups['web']
        - name: fail on web2
          ansible.builtin.fail: {msg: "deliberate"}
          when: inventory_hostname == 'web2'
          ignore_errors: true
    """
).strip()

_INVENTORY = textwrap.dedent(
    """
    all:
      children:
        web:
          hosts:
            web1: {ansible_connection: local}
            web2: {ansible_connection: local}
        db:
          hosts:
            db1: {ansible_connection: local}
    """
).strip()

#: Every character the format has to survive in a host name. Escaping rather
#: than validating is forced: `validate_host_name` does not refuse `|`, and a
#: host from a GIT inventory source never passes through it at all.
_HOSTILE_INVENTORY = textwrap.dedent(
    """
    all:
      hosts:
        "we|ird": {ansible_connection: local}
        "ti~lde": {ansible_connection: local}
        "per%cent": {ansible_connection: local}
        "plain": {ansible_connection: local}
    """
).strip()


def _run(root: pathlib.Path, *, inventory: str, play: str, tagged: bool, free: bool = False) -> str:
    """Drive ansible once and return its combined output.

    HOME is NOT relocated, for the reason `test_host_events_callback` records:
    it moves Python's user site-packages with it, so a user-site ansible stops
    importing and the script dies before parsing its arguments.
    """
    plugins = root / "cb"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_PLUGIN, plugins / _PLUGIN.name)
    (root / "inventory.yml").write_text(inventory)
    body = (
        play.replace("gather_facts: false", "gather_facts: false\n  strategy: free")
        if free
        else play
    )
    (root / "site.yml").write_text(body)

    env = dict(os.environ)
    env.update({"ANSIBLE_FORCE_COLOR": "0", "ANSIBLE_RETRY_FILES_ENABLED": "False"})
    if tagged:
        env["ANSIBLE_CALLBACK_PLUGINS"] = str(plugins)
        env["ANSIBLE_STDOUT_CALLBACK"] = "terrapod_stdout"
    completed = subprocess.run(  # noqa: S603
        [str(ANSIBLE), "-i", "inventory.yml", "site.yml"],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=300,
        check=False,
    )
    return completed.stdout.decode() + completed.stderr.decode()


def _canon(text: str, *roots: pathlib.Path) -> str:
    """Replace each run's own directory with a placeholder.

    Core 2.21 prints the playbook's ABSOLUTE path in its `[ERROR]` block
    (`Origin: <dir>/site.yml:11:7`, with a source excerpt and a column
    pointer), and 2.18 did not. The two runs below deliberately use separate
    directories so neither can see the other's artifacts, so that one value
    legitimately differs between them. Canonicalising it -- and nothing else --
    keeps the comparison byte-exact about everything ansible actually said.
    """
    for root in roots:
        text = text.replace(str(root), "<run>")
    return text


def _strip(text: str) -> str:
    """Remove the header and one prefix per line, as the traditional view does."""
    out: list[str] = []
    seen = False
    for line in text.split("\n"):
        if line == HEADER:
            seen = True
            continue
        if seen:
            at = line.find(DELIMITER)
            out.append(line[at + len(DELIMITER) :] if at >= 0 else line)
        else:
            out.append(line)
    return "\n".join(out)


def _hosts_of(text: str) -> list[str]:
    """Every distinct host field that appeared, in first-seen order."""
    seen: list[str] = []
    for line in text.split("\n"):
        at = line.find(DELIMITER)
        if at < 0:
            continue
        host = line[:at].strip()
        if host not in seen:
            seen.append(host)
    return seen


@needs_ansible
class TestWhatAnsiblePrintsThroughIt:
    def test_our_own_plugin_logs_no_failure_using_method_warning(self, tmp_path):
        """The regression guard for the defect the first probe run found.

        `default` reads its behaviour through `self.get_option(...)` --
        `display_ok_hosts`, `display_skipped_hosts`, `show_per_host_start`,
        `result_format` -- and an option is registered only for a plugin that
        DECLARES it. Without `extends_documentation_fragment` the subclass
        inherits the code and not the options, so each call raises and ansible
        renders it as a warning naming our plugin, swallowing the task output.

        It is pinned here rather than left to the byte-identity test below
        because the warnings go to stdout, so that test would fail with a diff
        of eight warning lines and no indication that OPTIONS were the cause.
        """
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        assert "Failure using method" not in out, out[:2000]

    def test_stripping_the_tag_reproduces_ansibles_own_bytes(self, tmp_path):
        """The traditional view IS ansible's rendering, not an imitation of it.

        Host-completion order varies run to run under the default strategy, so
        the comparison is over SORTED lines: what is being pinned is that no
        byte of ansible's own words is added, lost or altered, not the order
        three hosts happened to finish in.
        """
        a, b = tmp_path / "a", tmp_path / "b"
        plain = _run(a, inventory=_INVENTORY, play=_PLAY, tagged=False)
        tagged = _run(b, inventory=_INVENTORY, play=_PLAY, tagged=True)
        assert HEADER in tagged
        assert sorted(_canon(_strip(tagged), a, b).split("\n")) == sorted(
            _canon(plain, a, b).split("\n")
        )

    def test_a_multi_line_result_carries_its_host_on_every_line(self, tmp_path):
        """The continuation lines are the reason this is a callback.

        `ok: [web1] => {` names the host; the `"msg":` and `}` lines that follow
        do not, and in ansible's own output nothing attributes them.
        """
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        body = [ln for ln in out.split("\n") if '"msg": "hello from web1"' in ln]
        assert body, out[:2000]
        assert all(ln.split(DELIMITER)[0].strip() == "web1" for ln in body), body
        closing = [ln for ln in out.split("\n") if ln.strip().endswith("| }")]
        assert closing, "no continuation closing brace was tagged"

    def test_ignoring_carries_its_host(self, tmp_path):
        """One of the three classes a text parser cannot attribute."""
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        lines = [ln for ln in out.split("\n") if ln.rstrip().endswith("...ignoring")]
        assert lines, out[:2000]
        assert all(ln.split(DELIMITER)[0].strip() == "web2" for ln in lines), lines

    def test_banners_and_the_recap_carry_the_hostless_marker(self, tmp_path):
        """Context, not noise: a filtered view still has to show these.

        The marker is explicit rather than "no prefix" because the absence of a
        prefix is also what an older runner's log looks like, and the viewer
        must not read "belongs to every host" as "this log is untagged".
        """
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        for needle in ("TASK [a multi-line result]", "PLAY RECAP"):
            lines = [ln for ln in out.split("\n") if needle in ln]
            assert lines, f"{needle} not found"
            assert all(ln.split(DELIMITER)[0].strip() == HOSTLESS for ln in lines), lines

    def test_a_host_name_cannot_break_the_format(self, tmp_path):
        """Escaped, so the delimiter survives and nothing collides with the marker.

        `we|ird` carries the delimiter itself; `ti~lde` would otherwise be
        indistinguishable from the host-less marker; `per%cent` proves `%` is
        escaped FIRST, or the other two would be double-escaped.
        """
        out = _run(tmp_path, inventory=_HOSTILE_INVENTORY, play=_PLAY, tagged=True)
        hosts = _hosts_of(out)
        assert "we%7Cird" in hosts, hosts
        assert "ti%7Elde" in hosts, hosts
        assert "per%25cent" in hosts, hosts
        # The common case stays legible -- escaping must not tax an ordinary name.
        assert "plain" in hosts, hosts
        # And nothing escaped can be mistaken for the marker.
        assert [h for h in hosts if h == HOSTLESS] == [HOSTLESS]

    def test_the_free_strategy_is_tagged_too(self, tmp_path):
        """Where ansible's own output is LEAST attributable, measured at 6 of 24."""
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True, free=True)
        assert "Failure using method" not in out, out[:2000]
        assert {"web1", "web2", "db1"} <= set(_hosts_of(out))
