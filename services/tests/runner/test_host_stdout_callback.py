"""The stdout callback, driven through real ansible (#2144, #2513).

The plugin REPLACES ansible's renderer, so every property worth asserting is a
property of what ansible actually prints through it. A shape assertion about the
class establishes none of them, and the first probe run proved why: the plugin
loaded, ran, and emitted eight `[WARNING]: Failure using method (...) in callback
plugin` lines and no task output at all, because a subclass with no
`DOCUMENTATION` inherits `default`'s code and not its OPTIONS. Nothing short of
running it would have shown that.

v2 made the **block** the unit of output -- one per task per host, carrying the
host, the task, the result and the time -- in place of v1's host prefix on every
line. With no second format to fall back to (user, 2026-11-11: *"We own the
ansible output... we are entitled to design our own output structure"*), the
obligation that replaces "keep a traditional view" is **completeness**, and
`test_nothing_ansible_printed_is_lost` is the test that holds it: it drives the
same play twice, once through `default` and once through this, and requires
every line of ansible's own output to survive.

The rest pin properties no text parser over ansible's combined stdout can have,
which is the whole argument for a callback rather than a parser:

* a multi-line result, a failure's `[ERROR]` block and its `Origin:` path, the
  dumped result and the trailing `...ignoring` all land INSIDE the failing
  host's block -- in ansible's own output the first three are printed BEFORE
  the line that names the host, and the last names none;
* every block header carries its host structurally, so a host name that would
  break the grammar is escaped rather than hoped about;
* play-level output -- the PLAY banner, the PLAY RECAP -- carries an explicit
  host-less marker, so a filtered view can show it as context and a viewer can
  still tell a framed log from an unframed one.
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

#: The format's literals, read from the plugin rather than restated, so a change
#: to either side cannot pass this file silently.
_SRC = _PLUGIN.read_text()


def _literal(name: str) -> str:
    match = re.search(rf'^{name} = "(?P<v>[^"]*)"', _SRC, re.M)
    assert match is not None, f"{name} is gone from the plugin -- has it been renamed?"
    return match.group("v")


HEADER = _literal("HEADER")
HOSTLESS = _literal("HOSTLESS")
PROGRESS_STATUS = _literal("PROGRESS_STATUS")
START_STATUS = _literal("START_STATUS")
INDENT = _literal("INDENT")
EXAMPLE_BLOCK = _literal("EXAMPLE_BLOCK")

#: The block grammar, restated here exactly as the viewer restates it in
#: TypeScript and for the same reason -- the plugin runs under the execution
#: environment's Python and cannot be imported. `test_the_example_block_parses`
#: is what ties this restatement to the plugin's own example of it.
_BLOCK = re.compile(
    r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}[^\]]*\]\s+(\S+)\s+(\S+)(?:\s+(.*))?$"
)
_LEADING_SGR = re.compile(r"^(?:\x1b\[[0-9;]*m)+")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


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

#: A role, so the block label is exercised as `role : task` rather than a bare
#: name -- which is what the issue asks the label to carry and what
#: `Task.get_name()` already returns for a task inside one.
_ROLE = textwrap.dedent(
    """
    - name: Deploy the application
      ansible.builtin.debug:
        msg: |-
          hello from {{ inventory_hostname }}
          second line
    """
).strip()

_PLAY = textwrap.dedent(
    """
    - name: blocks
      hosts: all
      gather_facts: false
      roles:
        - platform
      tasks:
        - name: skip on db1
          ansible.builtin.debug: {msg: "web only"}
          when: inventory_hostname in groups['web']
        - name: a loop
          ansible.builtin.debug: {msg: "item {{ item }}"}
          loop: [alpha, beta]
          when: inventory_hostname == 'web1'
        - name: a changed result
          ansible.builtin.command: "true"
          changed_when: true
          when: inventory_hostname == 'web1'
        - name: fail on web2 with stdout and stderr
          ansible.builtin.shell: "echo out-line-one; echo err-line >&2; exit 7"
          when: inventory_hostname == 'web2'
          ignore_errors: true
        - name: unreachable survives
          ansible.builtin.ping:
          ignore_unreachable: true
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
        dead:
          hosts:
            hdead:
              ansible_host: 127.0.0.1
              ansible_port: 1
              ansible_connection: ssh
              ansible_ssh_common_args: "-o ConnectTimeout=2 -o StrictHostKeyChecking=no"
    """
).strip()

#: Every character the format has to survive in a host name. Escaping rather
#: than validating is forced: `validate_host_name` does not refuse `|`, and a
#: host from a GIT inventory source never passes through it at all. The SPACE
#: is the one the v2 grammar actually rests on -- the host is the first
#: whitespace-delimited field of a block header.
_HOSTILE_INVENTORY = textwrap.dedent(
    """
    all:
      hosts:
        "we|ird": {ansible_connection: local}
        "ti~lde": {ansible_connection: local}
        "per%cent": {ansible_connection: local}
        "spa ced": {ansible_connection: local}
        "plain": {ansible_connection: local}
    """
).strip()


def _run(
    root: pathlib.Path,
    *,
    inventory: str,
    play: str,
    tagged: bool,
    free: bool = False,
    color: bool = False,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Drive ansible once and return its combined output.

    HOME is NOT relocated, for the reason `test_host_events_callback` records:
    it moves Python's user site-packages with it, so a user-site ansible stops
    importing and the script dies before parsing its arguments.
    """
    plugins = root / "cb"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_PLUGIN, plugins / _PLUGIN.name)
    role = root / "roles" / "platform" / "tasks"
    role.mkdir(parents=True, exist_ok=True)
    (role / "main.yml").write_text(_ROLE)
    (root / "inventory.yml").write_text(inventory)
    body = (
        play.replace("gather_facts: false", "gather_facts: false\n  strategy: free")
        if free
        else play
    )
    (root / "site.yml").write_text(body)

    env = dict(os.environ)
    env.update(
        {
            "ANSIBLE_FORCE_COLOR": "1" if color else "0",
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
            "ANSIBLE_NOCOWS": "1",
        }
    )
    if tagged:
        env["ANSIBLE_CALLBACK_PLUGINS"] = str(plugins)
        env["ANSIBLE_STDOUT_CALLBACK"] = "terrapod_stdout"
    env.update(extra_env or {})
    completed = subprocess.run(  # noqa: S603
        [str(ANSIBLE), "-i", "inventory.yml", "site.yml"],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=600,
        check=False,
    )
    return completed.stdout.decode() + completed.stderr.decode()


#: A TASK/PLAY banner in ansible's own rendering. Its content is the task name,
#: which the block header carries instead.
_BANNER = re.compile(r"^(TASK|RUNNING HANDLER|PLAY|PLAY RECAP)\b.*\*+$")

#: `default`'s own status line, whose host and result the block header carries.
_STATUS = re.compile(r"^(ok|changed|skipping|failed|fatal|included): \[[^\]]*\]")

#: The item label, which the block header carries in its own label.
_ITEM = re.compile(r"^\(item=[^)]*\)\s*(?:=>\s*)?")

#: `delta`, `start` and `end` differ between two runs of the same play, as does
#: the directory each ran in. Canonicalising exactly those -- and nothing else
#: -- keeps the completeness comparison byte-exact about what ansible said.
_VOLATILE = (
    (re.compile(r'"(delta|end|start)": "[^"]*"'), r'"\1": "<t>"'),
    (re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+"), "<t>"),
)


def _canon(text: str, *roots: pathlib.Path) -> str:
    for root in roots:
        text = text.replace(str(root), "<run>")
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    return text


class Block:
    """One emitted block: its header's fields, and the lines indented under it."""

    def __init__(self, host: str | None, status: str, label: str, header: str) -> None:
        self.host = host
        self.status = status
        self.label = label
        self.header = header
        #: Body lines with any leading colour removed, for reading content.
        self.body: list[str] = []
        #: The same lines verbatim. Kept separately because "the whole block is
        #: coloured" is a property of the BYTES, and an assertion over the
        #: stripped form cannot see a body that lost its colour.
        self.raw_body: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join([self.header, *self.body])


def _blocks(text: str) -> list[Block]:
    """Every block in a framed log, in order. Mirrors the viewer's parser."""
    out: list[Block] = []
    seen = False
    for line in text.split("\n"):
        if line == HEADER:
            seen = True
            continue
        if not seen:
            continue
        probe = _LEADING_SGR.sub("", line)
        match = _BLOCK.match(probe)
        if match is not None:
            field, status, label = match.group(1), match.group(2), match.group(3) or ""
            host = None if field == HOSTLESS else field
            out.append(Block(host, status, label, line))
            continue
        if out and probe.startswith(" "):
            out[-1].body.append(probe)
            out[-1].raw_body.append(line)
    return out


def _for(blocks: list[Block], host: str, label_contains: str) -> list[Block]:
    return [b for b in blocks if b.host == host and label_contains in b.label]


def _callback_complaints(text: str) -> list[str]:
    """Every line where ansible names OUR plugin in a diagnostic.

    DERIVED rather than matched against a phrase, because the phrase moved. The
    guard was written against core 2.21, which says

        [WARNING]: Failure using method (v2_runner_on_ok) in callback plugin ...

    and **core 2.22 says something else entirely**:

        [WARNING]: Callback dispatch 'v2_runner_on_ok' failed for plugin
        'terrapod_stdout': 'display_ok_hosts'

    so the literal matcher is a gate that cannot fail on a current core --
    measured by removing `extends_documentation_fragment` on 2.22 and watching
    it pass. The plugin ships to ten EE lines and an operator's own image, so
    "whichever wording this core uses" is the only predicate worth having: the
    property is that ansible never complains about us, and it names us when it
    does.
    """
    return [
        line
        for line in text.split("\n")
        if "terrapod_stdout" in line and ("WARNING" in line or "ERROR" in line)
    ]


@pytest.fixture(scope="module")
def framed_run(tmp_path_factory) -> tuple[str, pathlib.Path]:
    """One real run through the plugin, shared: every assertion reads this text.

    The directory comes back with it because core prints the playbook's
    ABSOLUTE path in its `[ERROR]` block (`Origin: <dir>/site.yml:11:7`), and
    the completeness comparison below runs in a directory of its own so neither
    run can see the other's artifacts -- so that one value legitimately differs
    and has to be canonicalised on both sides.
    """
    if ANSIBLE is None:
        pytest.skip("no working ansible-playbook")
    root = tmp_path_factory.mktemp("framed")
    return _run(root, inventory=_INVENTORY, play=_PLAY, tagged=True), root


@pytest.fixture(scope="module")
def framed(framed_run) -> str:
    return framed_run[0]


@needs_ansible
class TestWhatAnsiblePrintsThroughIt:
    def test_ansible_never_complains_about_our_own_plugin(self, framed):
        """The regression guard for the defect the first probe run found.

        `default` reads its behaviour through `self.get_option(...)` --
        `display_ok_hosts`, `display_skipped_hosts`, `show_per_host_start`,
        `result_format` -- and an option is registered only for a plugin that
        DECLARES it. Without `extends_documentation_fragment` the subclass
        inherits the code and not the options, so each call raises and ansible
        renders it as a warning naming our plugin, swallowing the task output.

        It is pinned separately from everything below because the warnings go
        to stdout: a completeness diff would fail with a wall of warning lines
        and no indication that OPTIONS were the cause.
        """
        assert not _callback_complaints(framed), _callback_complaints(framed)[:5]

    def test_the_example_block_parses_to_the_fields_it_claims(self):
        """The plugin's own example, read back through the grammar.

        `EXAMPLE_BLOCK` is the one literal the viewer's TypeScript parser and
        this file both read out of the plugin, because neither can import it.
        Tying it to the grammar here and to real output below is what makes the
        three agree without a shared constant.
        """
        match = _BLOCK.match(EXAMPLE_BLOCK)
        assert match is not None, f"the plugin's own example does not parse: {EXAMPLE_BLOCK!r}"
        assert match.group(1) == "web1"
        assert match.group(2) == "changed"
        assert match.group(3) == "platform : Deploy"
        assert INDENT.strip() == "", "a body line's indent must be whitespace, or it parses as one"

    def test_every_block_carries_host_task_result_and_time(self, framed):
        """The four fields the issue asks every block to be guaranteed to have."""
        blocks = _blocks(framed)
        assert blocks, framed[:2000]
        results = [b for b in blocks if b.host is not None]
        assert results, "no block was attributed to a host"
        for block in results:
            assert block.status, block.header
            assert block.label, block.header
            # `[HH:MM:SS +dur]` -- the duration comes from the callback itself,
            # not from a `profile_tasks` line printed beside it.
            assert re.match(r"^\[\d{2}:\d{2}:\d{2} \+[0-9.]+[a-z0-9]*\]", block.header), (
                block.header
            )

    def test_a_block_is_one_task_on_one_host(self, framed):
        """The point of the rewrite: the task and its result are ONE item."""
        deploy = _for(_blocks(framed), "web1", "Deploy the application")
        assert len(deploy) == 1, [b.header for b in deploy]
        block = deploy[0]
        assert block.label == "platform : Deploy the application", block.label
        assert block.status in {"ok", "changed"}, block.status
        # The multi-line result is the block's body, and carries no host of its
        # own in ansible's output -- which is why this is a callback.
        assert any("hello from web1" in line for line in block.body), block.body

    def test_a_failure_carries_its_error_block_dump_and_ignoring_inside_it(self, framed):
        """The three classes a text parser cannot attribute, all in one block.

        In ansible's own output the `[ERROR]:` block and its `Origin:` path are
        printed BEFORE the `fatal: [web2]` line that names the host, and
        `...ignoring` after it names none at all.
        """
        failed = _for(_blocks(framed), "web2", "fail on web2")
        assert len(failed) == 1, [b.header for b in failed]
        block = failed[0]
        assert block.status == "FAILED", block.header
        body = "\n".join(block.body)
        assert "[ERROR]" in body, body
        assert "Origin:" in body, body
        assert "out-line-one" in body, "the module's stdout must be inside its block"
        assert "err-line" in body, "the module's stderr must be inside its block"
        assert "...ignoring" in body, "the ignore must be attributed, not left floating"

    def test_each_loop_item_is_its_own_block_naming_its_item(self, framed):
        """ "plus the item for loop results" -- per item, not one block for all."""
        items = _for(_blocks(framed), "web1", "a loop")
        labels = sorted(b.label for b in items)
        assert labels == ["a loop (item=alpha)", "a loop (item=beta)"], labels
        for block in items:
            item = block.label.split("item=")[1].rstrip(")")
            assert any(f"item {item}" in line for line in block.body), block.body

    def test_a_skip_and_an_unreachable_each_get_their_own_status(self, framed):
        blocks = _blocks(framed)
        skipped = _for(blocks, "db1", "skip on db1")
        assert [b.status for b in skipped] == ["skipped"], [b.header for b in skipped]
        unreachable = _for(blocks, "hdead", "unreachable survives")
        assert [b.status for b in unreachable] == ["UNREACHABLE"], [b.header for b in unreachable]

    def test_play_level_output_is_a_hostless_block(self, framed):
        """Context, not noise: a filtered view still has to show these.

        The marker is explicit rather than "no host field" because the absence
        of one is also what an older runner's log looks like, and the viewer
        must not read "belongs to every host" as "this log is unframed".
        """
        hostless = [b for b in _blocks(framed) if b.host is None]
        kinds = {b.status for b in hostless}
        assert "play" in kinds, [b.header for b in hostless]
        assert "recap" in kinds, [b.header for b in hostless]
        recap = next(b for b in hostless if b.status == "recap")
        # The whole recap is one block, and every host is in it exactly once --
        # `default` emits each row twice, once for the screen and once for its
        # log file, and a frame that kept both would double it.
        for host in ("web1", "web2", "db1", "hdead"):
            rows = [line for line in recap.body if line.strip().startswith(host)]
            assert len(rows) == 1, (host, recap.body)

    def test_a_task_banner_is_not_emitted_beside_the_block_that_names_it(self, framed):
        """The one thing v2 drops, and nothing is lost by it.

        A TASK banner's entire content is the task name, which every block
        carries in its label. Keeping it would reinstate the "the task and its
        result are still two items" complaint this exists to fix.
        """
        assert "TASK [" not in _ANSI.sub("", framed), framed[:2000]
        assert "Deploy the application" in framed, "...but the task name is still there"

    def test_nothing_ansible_printed_is_lost(self, framed_run, tmp_path):
        """The obligation that replaced "keep a traditional view".

        Every line of ansible's own rendering has to survive into the block
        format. Two things legitimately do not appear verbatim, and both are
        carried by the block header instead: the TASK banner, and `default`'s
        own `status: [host]` lead -- of which everything after the ` => ` is
        the dumped result and MUST still be found.

        Compared over sorted lines because host-completion order varies run to
        run; what is pinned is that no byte of ansible's own words is lost, not
        the order three hosts happened to finish in.
        """
        framed, framed_root = framed_run
        plain = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=False)
        haystack = _canon(_ANSI.sub("", framed), framed_root, tmp_path)
        missing = []
        for raw in _ANSI.sub("", plain).split("\n"):
            line = _canon(raw.strip(), framed_root, tmp_path)
            if not line or _BANNER.match(line):
                continue
            lead = _STATUS.match(line)
            if lead is not None:
                at = line.find(" => ")
                if at < 0:
                    continue
                line = _ITEM.sub("", line[at + 4 :]).strip()
                if not line:
                    continue
            if line not in haystack:
                missing.append(line)
        assert not missing, missing[:10]

    def test_a_host_name_cannot_break_the_format(self, tmp_path):
        """Escaped, so the grammar holds and nothing collides with the marker.

        `we|ird` carried the v1 delimiter; `ti~lde` would otherwise be
        indistinguishable from the host-less marker; `per%cent` proves `%` is
        escaped FIRST, or the other two would be double-escaped; and `spa ced`
        is the one v2 actually rests on -- the host is the first
        whitespace-delimited field, so a space would split it into a host and
        a result.
        """
        out = _run(tmp_path, inventory=_HOSTILE_INVENTORY, play=_PLAY, tagged=True)
        hosts = {b.host for b in _blocks(out) if b.host is not None}
        assert {"we%7Cird", "ti%7Elde", "per%25cent", "spa%20ced"} <= hosts, hosts
        # The common case stays legible -- escaping must not tax an ordinary name.
        assert "plain" in hosts, hosts
        # And nothing escaped can be mistaken for the marker.
        assert HOSTLESS not in hosts, hosts

    def test_the_free_strategy_is_framed_too(self, tmp_path):
        """Where ansible's own output is LEAST attributable, measured at 6 of 24.

        It is also where a duration taken from "when the task started" would be
        wrong, because several tasks are in flight at once -- which is why the
        clock is per host AND per task.
        """
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True, free=True)
        assert not _callback_complaints(out), _callback_complaints(out)[:5]
        assert {"web1", "web2", "db1"} <= {b.host for b in _blocks(out) if b.host}

    def test_the_whole_block_is_coloured_and_every_line_closes_its_own_colour(self, tmp_path):
        """The colour applies to the block, not only to a status line.

        Per LINE, which is the property that matters rather than a nicety:
        ansible's `stringc` wraps each line in its own SGR pair, so a filtered
        view that keeps some blocks and drops others can never leave a colour
        unterminated.
        """
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True, color=True)
        opened = [ln for ln in out.split("\n") if ln.startswith("\x1b[")]
        assert opened, out[:2000]
        assert all(ln.endswith("\x1b[0m") for ln in opened), [
            ln for ln in opened if not ln.endswith("\x1b[0m")
        ][:5]
        # A failure's WHOLE block is red -- body included, which is the change
        # v2 makes and the half an assertion about the header alone cannot see.
        failed = _for(_blocks(out), "web2", "fail on web2")
        assert failed, out[:2000]
        block = failed[0]
        assert block.header.startswith("\x1b[0;31m"), block.header[:40]
        assert block.raw_body, "the failure's own output is what has to be coloured"
        uncoloured = [ln for ln in block.raw_body if not ln.startswith("\x1b[0;31m")]
        assert not uncoloured, uncoloured[:5]
        # ...and the ok blocks are a different colour, so "coloured by the
        # RESULT" is pinned rather than "coloured".
        ok = _for(_blocks(out), "web1", "Deploy the application")
        assert ok and ok[0].header.startswith("\x1b[0;32m"), ok[0].header[:40] if ok else out[:800]

    def test_another_callbacks_output_is_left_exactly_as_it_was(self, tmp_path):
        """ "whatever an operator's own profile_tasks / timer callback emits".

        It is emitted verbatim, which is what "left as it is" means, and the
        parser reads an unindented non-block line back as host-less -- so it
        still shows in every filtered view rather than being lost to one.
        """
        marker = "Wednesday 11 November 2026  10:28:27 +0000 (0:00:00.018) ****"
        plugins = tmp_path / "cb"
        plugins.mkdir(parents=True, exist_ok=True)
        (plugins / "fake_profile.py").write_text(
            textwrap.dedent(
                f'''
                """A stand-in for `ansible.posix.profile_tasks`, which is not in core."""

                from __future__ import annotations

                from ansible.plugins.callback import CallbackBase

                DOCUMENTATION = """
                    name: fake_profile
                    type: aggregate
                    short_description: a profile_tasks stand-in
                    description: [prints a timing line at every task start]
                """


                class CallbackModule(CallbackBase):
                    CALLBACK_VERSION = 2.0
                    CALLBACK_TYPE = "aggregate"
                    CALLBACK_NAME = "fake_profile"
                    CALLBACK_NEEDS_ENABLED = True

                    def v2_playbook_on_task_start(self, task, is_conditional):
                        self._display.display({marker!r})
                '''
            ).strip()
        )
        out = _run(
            tmp_path,
            inventory=_INVENTORY,
            play=_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_CALLBACKS_ENABLED": "fake_profile"},
        )
        assert marker in out, out[:2000]
        # Verbatim: on its own line, with nothing added in front of it.
        assert any(line == marker for line in out.split("\n")), [
            ln for ln in out.split("\n") if marker in ln
        ]
        # And it does not become a block, nor get absorbed into the one above.
        assert all(marker not in b.header for b in _blocks(out))
        assert all(marker not in "\n".join(b.body) for b in _blocks(out))


#: A play whose one task is slow enough to outlive a short notice threshold.
#: `command: sleep` rather than `pause`, because `pause` is a controller-side
#: action plugin that opens no connection and so is never "running on a host".
_SLOW_PLAY = textwrap.dedent(
    """
    - name: slow
      hosts: all
      gather_facts: false
      tasks:
        - name: a slow task
          ansible.builtin.command: "sleep 3"
          changed_when: false
    """
).strip()

_SLOW_INVENTORY = textwrap.dedent(
    """
    all:
      hosts:
        web1: {ansible_connection: local}
        web2: {ansible_connection: local}
    """
).strip()


def _running(text: str) -> list[Block]:
    """Every still-running notice, in order."""
    return [b for b in _blocks(text) if b.status == PROGRESS_STATUS]


def _starts(text: str) -> list[Block]:
    """Every task-START notice, in order."""
    return [b for b in _blocks(text) if b.status == START_STATUS]


@needs_ansible
class TestStillRunningNotices:
    """The progress signal (#2532).

    Driven through real ansible rather than asserted on the formatter, because
    the property is about WHEN something is emitted relative to work happening
    on another thread -- which a unit test of the line builder cannot see at
    all. The plugin's own recorded trap is the same shape: a callback whose
    options are not declared loads, runs, and emits nothing but warnings.
    """

    def test_a_slow_task_says_it_is_still_running(self, tmp_path):
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "1"},
        )
        notices = _running(out)
        assert notices, f"a 3s task produced no still-running notice:\n{out}"
        assert all("a slow task" in b.label for b in notices), [b.label for b in notices]

    def test_disabling_it_produces_none(self, tmp_path):
        """The control. Without this, the test above passes on a plugin that
        emits a notice unconditionally, which is not the feature."""
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "0"},
        )
        assert _running(out) == [], f"notices emitted while disabled:\n{out}"

    def test_a_threshold_longer_than_the_task_produces_none(self, tmp_path):
        """No spurious notice on a play shorter than the threshold.

        This does NOT establish that the threshold COMPARISON is honoured, and
        the mutation proves it: dropping the comparison entirely leaves this
        green, because the threshold doubles as the tick interval, so at 30s the
        ticker never wakes inside a 4s play at all. The comparison is pinned by
        `test_a_notice_never_reports_less_than_the_threshold` instead.
        """
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "30"},
        )
        assert _running(out) == [], f"notice before the threshold:\n{out}"

    def test_aggregated_counts_the_hosts_and_is_host_less(self, tmp_path):
        """One notice per TASK, carrying the host count -- not one per host.

        This is the shape decision: `per_host` scales with the inventory, so on
        a large fleet it is a wall rather than a progress signal.
        """
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "1"},
        )
        notices = _running(out)
        assert notices, out
        assert all(b.host is None for b in notices), [b.host for b in notices]
        assert any("(2 hosts)" in b.label for b in notices), [b.label for b in notices]

    def test_per_host_mode_names_each_host_instead(self, tmp_path):
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={
                "ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "1",
                "ANSIBLE_TERRAPOD_PROGRESS_MODE": "per_host",
            },
        )
        notices = _running(out)
        assert notices, out
        assert {b.host for b in notices} <= {"web1", "web2"}, [b.host for b in notices]
        assert all(b.host is not None for b in notices), [b.host for b in notices]
        assert not any("host)" in b.label or "hosts)" in b.label for b in notices)

    def test_no_notice_follows_the_recap(self, tmp_path):
        """The ticker is stopped and WAITED FOR before the recap is written.

        Without the join, a notice can be printed after the play has finished,
        saying a task is in flight on a play that is over.
        """
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "1"},
        )
        statuses = [b.status for b in _blocks(out)]
        assert "recap" in statuses, out
        assert PROGRESS_STATUS not in statuses[statuses.index("recap") :], statuses

    def test_a_notice_never_reports_less_than_the_threshold(self, tmp_path):
        """The threshold comparison itself, asserted on the figure emitted.

        A task that starts AFTER the ticker has is the only case where the
        comparison is observable from outside -- the fast task below pushes the
        slow one past the ticker's own phase -- and the elapsed figure in the
        header is the direct evidence. Dropping the comparison lets a notice
        report a task that has barely begun, which this catches and a count of
        notices does not.
        """
        # The FIRST task is slow too, deliberately. A quick one leaves the
        # second starting ~0.05s after the ticker, so a notice that ignored the
        # threshold would report ~1.95s -- which `f"{seconds:.1f}s"` rounds to
        # "2.0s" and the assertion below cannot tell from a legitimate one.
        # Three seconds of task A puts task B a clear ~1.0s into a tick.
        play = textwrap.dedent(
            """
            - name: slow
              hosts: all
              gather_facts: false
              tasks:
                - name: "the first slow task"
                  ansible.builtin.command: "sleep 3"
                  changed_when: false
                - name: "a slow task"
                  ansible.builtin.command: "sleep 5"
                  changed_when: false
            """
        ).strip()
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=play,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "2"},
        )
        notices = _running(out)
        assert notices, out
        seen = []
        for block in notices:
            found = re.search(r"\+([0-9.]+)s\]", block.header)
            assert found, block.header
            seen.append(float(found.group(1)))
        assert all(v >= 2.0 for v in seen), f"a notice reported under the threshold: {seen}"

    def test_a_notice_is_written_through_the_unwrapped_display(self):
        """Structural, because the race it guards cannot be forced from outside.

        `_tp_install` replaces `self._display.display` with a wrapper that
        CAPTURES into `_tp_frames` so a block can be assembled from what ansible
        printed. A notice raised while a frame is open would therefore be
        swallowed INTO that block rather than printed. `_tp_frames` is only
        non-empty during a hook, so whether a given notice collides is a race --
        driving it proves nothing either way, which the mutation confirmed by
        surviving a full run. So the property is asserted on the source.
        """
        body = _SRC.split("def _tp_tick_emit")[1].split("\n    def ")[0]
        assert "self._tp_inner_display(" in body, body
        assert "self._display.display(" not in body, (
            "a still-running notice goes through the WRAPPED display, so it will "
            "be captured into whatever block is open instead of being printed"
        )

    def test_the_options_load_without_warnings(self, tmp_path):
        """A subclass inherits the CODE and not the OPTIONS.

        The recorded failure: a `default`-subclassing callback with options it
        has not declared loads, runs, and turns every hook into a
        `[WARNING]: Failure using method ...` line with no task output at all.
        Declaring two new options is exactly the change that can reintroduce it.
        """
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "1"},
        )
        assert "Failure using method" not in out, out


@needs_ansible
class TestTaskStartNotices:
    """A task is announced when it STARTS (#2532).

    The third of the issue's three signals, and the one that was missing:
    `_tp_banner` drops ansible's own `TASK [...]` banner -- correctly, since
    every result block carries the task name -- so after #2513 nothing said a
    task had begun. On a task slower than the threshold the first line a
    reader saw was a still-running notice for a task they had never been told
    had started; on a quicker one, nothing at all until it was over.

    Driven through real ansible for the same reason the still-running class
    is: the property is about WHEN a line is emitted relative to the work,
    which no assertion on the line builder can see.
    """

    def test_every_task_is_announced_when_it_starts(self, tmp_path):
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        starts = _starts(out)
        assert starts, f"no task was announced at all:\n{out}"
        announced = {b.label for b in starts}
        for name in ("skip on db1", "a loop", "a changed result", "unreachable survives"):
            assert name in announced, f"{name!r} was never announced: {sorted(announced)}"

    def test_the_announcement_precedes_that_task_s_own_results(self, tmp_path):
        """The whole point: terraform says `Creating...` BEFORE it works.

        A notice that arrived after the result would be strictly worse than
        none, so the ordering is the property rather than the presence.
        """
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        blocks = _blocks(out)
        for label in ("a changed result", "unreachable survives"):
            first_start = next(
                (i for i, b in enumerate(blocks) if b.status == START_STATUS and b.label == label),
                None,
            )
            assert first_start is not None, f"{label!r} not announced"
            first_result = next(
                (
                    i
                    for i, b in enumerate(blocks)
                    if b.host is not None and b.status != START_STATUS and b.label == label
                ),
                None,
            )
            assert first_result is not None, f"{label!r} produced no result block"
            assert first_start < first_result, (
                f"{label!r} was announced at block {first_start}, AFTER its first "
                f"result at {first_result} -- the notice is useless behind the work"
            )

    def test_aggregated_announces_once_per_task_not_once_per_host(self, tmp_path):
        """The shape distinction, on a four-host inventory.

        A per-host announcement would multiply by the fleet, which is exactly
        what `terrapod_progress_mode` exists to avoid -- so this needs an
        inventory with more than one host or it cannot tell the two apart.
        """
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        starts = _starts(out)
        assert starts, f"no announcements:\n{out}"
        assert all(b.host is None for b in starts), (
            "an aggregated announcement carries a host, so it is per-host in "
            f"all but name: {[(b.host, b.label) for b in starts if b.host]}"
        )
        for label in {b.label for b in starts}:
            n = len([b for b in starts if b.label == label])
            assert n == 1, f"{label!r} announced {n} times in aggregated mode"

    def test_per_host_announces_once_per_host(self, tmp_path):
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_MODE": "per_host"},
        )
        starts = _starts(out)
        assert {b.host for b in starts} == {"web1", "web2"}, (
            f"per_host did not announce each host once: {[(b.host, b.label) for b in starts]}"
        )

    def test_a_task_quicker_than_the_threshold_is_still_announced(self, tmp_path):
        """The property that separates a START notice from the ticker.

        With a threshold longer than the whole play the ticker never fires, so
        a start notice gated on elapsed time would produce nothing -- which is
        precisely the gap this closes: a quick task used to be invisible until
        it had finished.
        """
        out = _run(
            tmp_path,
            inventory=_INVENTORY,
            play=_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "300"},
        )
        assert _running(out) == [], "the threshold was somehow reached; test is not measuring"
        assert _starts(out), f"a quick task was never announced:\n{out}"

    def test_disabling_the_notices_silences_the_start_too(self, tmp_path):
        """One switch for the whole progress experience.

        A start line an operator could not turn off would be a notice by
        another name, so `terrapod_progress_seconds: 0` governs both.
        """
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={"ANSIBLE_TERRAPOD_PROGRESS_SECONDS": "0"},
        )
        assert _starts(out) == [], f"announced while disabled:\n{out}"
        assert _running(out) == [], f"still-running notices while disabled:\n{out}"

    def test_ansibles_own_per_host_start_is_not_doubled(self, tmp_path):
        """`show_per_host_start` already renders a start block per host.

        Announcing on top of it would print each host twice. The guard is that
        our notice only fires when ansible's own produced nothing.
        """
        out = _run(
            tmp_path,
            inventory=_SLOW_INVENTORY,
            play=_SLOW_PLAY,
            tagged=True,
            extra_env={
                "ANSIBLE_TERRAPOD_PROGRESS_MODE": "per_host",
                "ANSIBLE_SHOW_PER_HOST_START": "True",
            },
        )
        starts = _starts(out)
        assert len(starts) == 2, (
            f"expected one start per host, got {len(starts)}: {[(b.host, b.label) for b in starts]}"
        )

    def test_a_start_notice_never_follows_the_recap(self, tmp_path):
        """Mirrors the still-running rule: nothing trails the summary."""
        out = _run(tmp_path, inventory=_INVENTORY, play=_PLAY, tagged=True)
        statuses = [b.status for b in _blocks(out)]
        assert "recap" in statuses, f"no recap block:\n{out}"
        assert START_STATUS not in statuses[statuses.index("recap") :], statuses
