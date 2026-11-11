"""The levels view over an inventory holding an inline `!vault` scalar (#2506).

Three claims, and the first two are claims about **ansible**, so they are made
by running it rather than by reasoning about it. Measured on core 2.21.6, the
version pinned in `services/pyproject.toml`:

* `ansible-inventory --list` never decrypts -- **not even when a vault password
  IS available**, measured both ways: a vaulted value comes back as
  `{"__ansible_vault": "<ciphertext>"}`. That is why resolution and configure
  runs have always worked with no secret in the API pod;
* `--graph --vars` prints every value as text, so it must decrypt, and with no
  secret it exits 1 -- discarding a `--list` that had already succeeded;
* so the attribution pass reads a **masked copy** of the scratch tree, in which
  every `!vault` scalar has been replaced by `<vaulted>`. The resolution itself
  is `--list`'s output and still carries the ciphertext.

The API holds no vault secret and is not going to. That is a trust-model
boundary, so this is the shape of the fix rather than passing the workspace's
`ansible_vault_auth` credential into the API's own read.

And one claim about **us**: ansible quotes the operator's own source lines back
in its errors, and those went verbatim into the refusal, the API response and
the UI. The lines around an offending value are the exposure -- a URL and a
username in the report this came from; a plaintext secret sitting beside a
vaulted one would travel the same way.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import shutil
import subprocess
import textwrap
import uuid

import pytest
import yaml

from terrapod.services import inventory_resolve
from terrapod.services.inventory_provenance import VAULTED_PLACEHOLDER, attribute
from terrapod.services.inventory_vault import mask_text, mask_vaulted_scalars


def _working_ansible() -> str | None:
    """A usable `ansible-inventory`, or None. Probed, not merely located.

    The same guard as `test_inventory_resolve.py`, and for the same reason: a
    stale shim on a developer's machine must not fail the suite, while the test
    image gates its own install at BUILD time so CI genuinely runs these.
    """
    found = shutil.which("ansible-inventory")
    if found is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603 -- the path came from `which`
            [found, "--version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if probe.returncode == 0 else None


def _working_vault() -> str | None:
    """`ansible-vault`, needed to MAKE a fixture rather than to read one.

    Separate from the one above because the two binaries can be present
    independently, and a test that encrypts needs this one specifically.
    """
    found = shutil.which("ansible-vault")
    if found is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603 -- the path came from `which`
            [found, "--version"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if probe.returncode == 0 else None


_ANSIBLE = _working_ansible()
_VAULT = _working_vault()
needs_ansible = pytest.mark.skipif(
    _ANSIBLE is None,
    reason="no WORKING ansible-inventory on PATH; the test image installs one",
)
needs_vault = pytest.mark.skipif(
    _ANSIBLE is None or _VAULT is None,
    reason="no WORKING ansible-inventory + ansible-vault on PATH",
)

#: The plaintext sitting next to the ciphertext in every fixture here. Ansible's
#: source excerpt quotes the lines AROUND the offending value, so this is what
#: must never reach a refusal -- the shape of the real report, where the leaked
#: lines were a URL and a username.
_NEIGHBOUR = "https://neighbour.example.invalid/not-for-your-eyes"


def _encrypt(plaintext: str, name: str, *, password_file: pathlib.Path) -> str:
    """`ansible-vault encrypt_string`, as the YAML an operator would commit.

    A real encryption rather than a canned ciphertext string: what is under
    test is what ansible does with the file, so the file has to be one ansible
    made.
    """
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    out = subprocess.run(  # noqa: S603 -- the path came from `which`
        [
            _VAULT,
            "encrypt_string",
            "--vault-password-file",
            str(password_file),
            plaintext,
            "--name",
            name,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return out.stdout


def _vaulted_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A config root whose one host var file holds an inline-vaulted value."""
    root = tmp_path / "repo"
    (root / "inv" / "host_vars").mkdir(parents=True)
    password = tmp_path / "pw"
    password.write_text("not-the-real-passphrase\n")
    (root / "inv" / "hosts.yml").write_text(
        "all:\n"
        "  children:\n"
        "    web:\n"
        "      vars:\n"
        "        shared: group-value\n"
        "      hosts:\n"
        "        web1.example.com:\n"
    )
    (root / "inv" / "host_vars" / "web1.example.com.yml").write_text(
        "---\n"
        "plain_var: hello\n"
        f"neighbour_url: {_NEIGHBOUR}\n"
        + _encrypt("not-a-real-secret", "db_password", password_file=password)
        + "\nafter_var: trailing\n"
    )
    return root


class TestMaskingTheScratchCopy:
    """What the masker does to a file, without ansible in the picture.

    The edit is spliced by byte offset from PyYAML's own node marks, so the
    claim is that everything OUTSIDE a vault scalar survives untouched -- which
    is the whole reason it is not a load-and-dump round trip.
    """

    #: A real ciphertext body is not needed to test the splice: the tag and a
    #: block scalar are what the composer reads. The body is deliberately short
    #: so the fixtures stay readable.
    _CIPHER = "$ANSIBLE_VAULT;1.1;AES256\n3133333700\n3161\n"

    @classmethod
    def _cipher(cls, indent: int) -> str:
        """The block scalar's body at `indent` spaces.

        A helper rather than a literal because a block scalar's continuation
        has to be indented MORE than the key that introduces it -- get that
        wrong and the fixture is simply not YAML, so the masker correctly
        declines it and the test passes for the wrong reason.
        """
        pad = " " * indent
        return "".join(f"{pad}{line}\n" for line in cls._CIPHER.splitlines())

    def test_a_vault_scalar_becomes_the_placeholder(self):
        out = mask_text(f"db_password: !vault |\n{self._cipher(2)}")
        assert out is not None
        assert "!vault" not in out
        assert f'db_password: "{VAULTED_PLACEHOLDER}"' in out
        assert "ANSIBLE_VAULT" not in out

    def test_the_deprecated_tag_is_masked_too(self):
        """`!vault-encrypted` is still parsed by core 2.21.6 -- it registers a
        constructor for it and merely deprecates it -- so masking only `!vault`
        would leave a file using it failing exactly as before."""
        out = mask_text(f"db_password: !vault-encrypted |\n{self._cipher(2)}")
        assert out is not None and "!vault" not in out
        assert VAULTED_PLACEHOLDER in out

    def test_a_vault_scalar_inside_a_sequence_is_masked(self):
        out = mask_text(f"secrets:\n  - !vault |\n{self._cipher(6)}  - plain\n")
        assert out is not None and "!vault" not in out
        assert "- plain" in out

    def test_everything_outside_the_span_survives_byte_for_byte(self):
        """The reason this is a splice rather than a round trip.

        A load-and-dump would lose the comments, expand the alias, re-quote at
        PyYAML's discretion, and fail outright on `!unsafe`, which has no safe
        constructor. All of that is a change the operator did not make to a file
        ansible is about to parse.
        """
        source = (
            "# a leading comment\n"
            "---\n"
            "defaults: &base\n"
            "  retries: 3          # trailing comment\n"
            "web1:\n"
            "  <<: *base\n"
            f"  secret: !vault |\n{self._cipher(4)}"
            '  unsafe_value: !unsafe "{{ not_a_template }}"\n'
            "  flow: {a: 1, b: 2}\n"
            "  after: still-here\n"
            "---\n"
            "# a SECOND document\n"
            "other: doc\n"
        )
        out = mask_text(source)
        assert out is not None
        for survivor in (
            "# a leading comment",
            "# trailing comment",
            "&base",
            "<<: *base",
            '!unsafe "{{ not_a_template }}"',
            "flow: {a: 1, b: 2}",
            "after: still-here",
            "# a SECOND document",
        ):
            assert survivor in out, survivor
        # It still parses, and as TWO documents. `compose_all` rather than
        # `safe_load_all` on purpose: `!unsafe` has no safe constructor, which
        # is itself the point.
        assert len(list(yaml.compose_all(out))) == 2

    def test_the_line_count_is_preserved(self):
        """So every later line keeps its number and an error ansible reports
        afterwards still points where the operator would look."""
        source = f"a: 1\nsecret: !vault |\n{self._cipher(2)}b: 2\n"
        out = mask_text(source)
        assert out is not None
        assert out.count("\n") == source.count("\n")
        assert out.splitlines()[-1] == "b: 2"

    def test_a_file_with_no_vault_tag_is_left_alone(self):
        """None rather than the unchanged text, so the caller can tell "nothing
        to do" from "rewritten identically" and leave the file's mtime alone."""
        assert mask_text("a: 1\nb: 2\n") is None

    def test_a_file_that_is_not_yaml_is_left_alone(self):
        """A file that is not YAML cannot be carrying a YAML tag, and a file
        that IS YAML and is broken is ansible's to report -- rewriting it here
        would only change which error the operator sees."""
        assert mask_text("not yaml: [ !vault\n  oops") is None

    def test_the_walk_rewrites_only_the_files_that_need_it(self, tmp_path):
        tree = tmp_path / "root"
        (tree / "group_vars").mkdir(parents=True)
        plain = tree / "group_vars" / "plain.yml"
        plain.write_text("a: 1\n")
        vaulted = tree / "group_vars" / "secret.yml"
        vaulted.write_text(f"s: !vault |\n{self._cipher(2)}")
        before = plain.read_bytes()

        assert mask_vaulted_scalars(tree) == 1
        assert plain.read_bytes() == before
        assert "!vault" not in vaulted.read_text()

    def test_a_suffix_no_vars_plugin_reads_is_skipped(self, tmp_path):
        """Scoped to what a vars plugin or the yaml inventory plugin will read,
        so a tree holding something large and unrelated is not read into memory
        on the off-chance it is YAML."""
        tree = tmp_path / "root"
        tree.mkdir()
        other = tree / "notes.txt"
        other.write_text(f"s: !vault |\n{self._cipher(2)}")
        assert mask_vaulted_scalars(tree) == 0
        assert "!vault" in other.read_text()


class TestTheLevelsViewSurvivesAVaultedVariable:
    """The issue's own acceptance, driven end to end through real ansible."""

    @needs_vault
    def test_the_graph_pass_fails_without_the_masking(self, tmp_path):
        """The premise, measured rather than assumed.

        Without this the three tests below would pass just as well against a
        version of ansible that had never had the problem, and the fix would be
        guarding nothing.
        """
        root = _vaulted_tree(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        with pytest.raises(inventory_resolve.InventoryResolutionFailed) as exc:
            asyncio.run(
                inventory_resolve.run_ansible_graph([root / "inv"], scratch=scratch, cwd=root)
            )
        assert "vault" in str(exc.value).lower(), str(exc.value)

        # ...while `--list` over the same tree succeeds and carries the
        # ciphertext, which is what makes discarding it the defect.
        listed = asyncio.run(
            inventory_resolve.run_ansible_inventory([root / "inv"], scratch=scratch, cwd=root)
        )
        hostvars = listed["_meta"]["hostvars"]["web1.example.com"]
        assert set(hostvars["db_password"]) == {"__ansible_vault"}

    @needs_vault
    def test_the_graph_pass_succeeds_once_the_tree_is_masked(self, tmp_path):
        root = _vaulted_tree(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()

        assert mask_vaulted_scalars(root) == 1
        graph = asyncio.run(
            inventory_resolve.run_ansible_graph([root / "inv"], scratch=scratch, cwd=root)
        )
        assert f"db_password = {VAULTED_PLACEHOLDER}" in graph
        # The attribution for everything else is untouched: the plain host var,
        # the group var, and the group's own level.
        assert "plain_var = hello" in graph
        assert "shared = host-value" not in graph
        assert "shared = group-value" in graph

    @needs_vault
    def test_the_whole_resolution_with_levels_succeeds(self, tmp_path, monkeypatch):
        """The read the issue reports failing: `--list` plus the levels view.

        Driven through `_resolve_uncached`, which is where the masking is
        sequenced, so this fails if the call moves to the wrong side of either
        ansible invocation.
        """
        root = _vaulted_tree(tmp_path)
        document = _resolve_with_levels(root, monkeypatch)

        assert "web1.example.com" in document["hosts"]
        levels = document["var-levels"]
        host = levels["web1.example.com"]
        assert host["db_password"] == VAULTED_PLACEHOLDER, levels
        assert host["plain_var"] == "hello"
        assert levels["@web"]["shared"] == "group-value"

    @needs_vault
    def test_the_resolution_still_carries_the_ciphertext(self, tmp_path, monkeypatch):
        """`--list` is what the resolution IS, and it is left untouched -- so a
        reader still sees that the value is vaulted rather than a bare
        placeholder that could be mistaken for its content."""
        root = _vaulted_tree(tmp_path)
        document = _resolve_with_levels(root, monkeypatch)
        value = document["hosts"]["web1.example.com"]["db_password"]
        assert set(value) == {"__ansible_vault"}
        assert value["__ansible_vault"].startswith("$ANSIBLE_VAULT")


def _resolve_with_levels(root: pathlib.Path, monkeypatch) -> dict:
    """`_resolve_uncached` over a materialised config root, levels requested.

    The fetch is stubbed because what is under test is everything AFTER a tree
    exists; the screening the real fetch performs is run here for real so the
    tree reaches ansible in the state production would hand it.
    """

    class _Workspace:
        id = uuid.uuid4()
        configure_enabled = True
        config_directory = ""
        inventory_paths = ["inv"]
        ansible_version = ""
        hard_limit = ""
        ansible_host_key_checking = True
        # Both off, which is the default, and set explicitly: these reach a
        # `bool(...)`, so an auto-attribute would read as TRUE and quietly put
        # the test on the dynamic path.
        dynamic_inventory_enabled = False
        inventory_scripts_enabled = False

    ws = _Workspace()
    skipped, executables = inventory_resolve._screen_config_root(root, allow_plugins=False)

    async def _fetch(db, workspace, git_sha, workdir):
        return inventory_resolve._ScreenedRoot(root=root, skipped=skipped, executables=executables)

    async def _declared(db, workspace_id):
        return {
            "hosts": [],
            "groups": [],
            "memberships": [],
            "children": [],
            "global_vars": {},
        }

    monkeypatch.setattr(inventory_resolve, "_fetch_vcs_source", _fetch)
    monkeypatch.setattr(inventory_resolve, "_declared_document", _declared)
    monkeypatch.setattr(inventory_resolve, "ansible_inventory_binary", lambda: _ANSIBLE)

    return asyncio.run(
        inventory_resolve._resolve_uncached(
            object(),
            ws.id,
            ws=ws,
            git_sha="abc123",
            limit=None,
            want_levels=True,
        )
    )


class TestTheMaskingIsSequencedCorrectly:
    """A positional gate, because no behavioural test can see the ORDER.

    Masking before `--list` would put the placeholder into the resolution
    itself, so the API would stop reporting that a value is vaulted at all; not
    masking at all restores the failure. Both leave `_resolve_uncached`
    returning a document, so only the source says which one is in place.

    The same shape as `TestEveryMaterialisedSourceIsScanned` in
    `test_inventory_resolve.py`, and for the same reason it reads the AST rather
    than searching the text: the masker is NAMED IN A COMMENT above the code it
    guards, so a substring search finds the prose first.
    """

    #: The three calls whose ORDER is the property. The masker is handed to
    #: `asyncio.to_thread` rather than called directly -- rule 13, it walks a
    #: tree and rewrites files -- so a gate reading `node.func` alone would
    #: report it as absent and fire on correct code.
    _WATCHED = ("run_ansible_inventory", "run_ansible_graph", "mask_vaulted_scalars")

    def _call_lines(self) -> dict[str, int]:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inventory_resolve._resolve_uncached)))
        lines: dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in self._WATCHED:
                lines.setdefault(str(name), node.lineno)
                continue
            # `asyncio.to_thread(fn, ...)`: the callee is the first argument.
            if name == "to_thread" and node.args and isinstance(node.args[0], ast.Name):
                handed = node.args[0].id
                if handed in self._WATCHED:
                    lines.setdefault(handed, node.lineno)
        return lines

    def test_the_resolution_runs_before_the_masking(self):
        """`--list`'s output IS the resolution, ciphertext and all."""
        lines = self._call_lines()
        assert "run_ansible_inventory" in lines, "the --list call moved or was renamed"
        assert "mask_vaulted_scalars" in lines, (
            "nothing masks the scratch tree, so the levels view fails on any "
            "inventory holding an inline !vault scalar (#2506)"
        )
        assert lines["run_ansible_inventory"] < lines["mask_vaulted_scalars"], (
            "the masking runs before --list, so the placeholder would reach the "
            "resolution itself and a vaulted value would stop reading as vaulted"
        )

    def test_the_masking_runs_before_the_attribution_pass(self):
        lines = self._call_lines()
        assert "run_ansible_graph" in lines, "the --graph --vars call moved or was renamed"
        assert lines["mask_vaulted_scalars"] < lines["run_ansible_graph"], (
            "the attribution pass reads the tree before it has been masked, so it "
            "still has to decrypt and still exits 1"
        )


class TestAnsibleSOwnSourceLinesNeverReachARefusal:
    """The second half of #2506, and unconditional rather than a vault case.

    Every parse error on this path quotes a file Terrapod fetched on the
    operator's behalf. The ciphertext is harmless; its NEIGHBOURS may not be.
    """

    def test_the_excerpt_is_removed_and_the_origin_is_kept(self):
        """Ansible's own shape, copied from a real run on core 2.21.6."""
        stderr = (
            "[ERROR]: Attempting to decrypt but no vault secrets found.\n"
            "Origin: /scratch/vcs/host_vars/web1.yml:5:14\n"
            "\n"
            "3 neighbour_url: https://internal.example/path\n"
            "4 neighbour_user: svc-account\n"
            "5 db_password: !vault |\n"
            "               ^ column 14\n"
        )
        out = inventory_resolve._without_source_excerpt(stderr)
        assert "neighbour_url" not in out
        assert "svc-account" not in out
        assert "^ column 14" not in out
        # The pointer survives, which is the whole of what an operator needs to
        # open the file themselves.
        assert "Origin: /scratch/vcs/host_vars/web1.yml:5:14" in out
        assert "Attempting to decrypt" in out

    def test_an_origin_with_no_excerpt_keeps_the_message_after_it(self):
        """The shape that a "drop the next block" matcher would eat.

        An `Origin:` with no line number is ALSO followed by a blank line, and
        what comes next is the next message rather than an excerpt -- measured
        on the commonest refusal there is, an unparseable source.
        """
        stderr = (
            "Failed to parse inventory with 'yaml' plugin.\n"
            "Origin: <inventory plugin 'yaml' with source '/scratch/20_bad.yml'>\n"
            "\n"
            "[WARNING]: Failed to parse inventory with 'ini' plugin: Invalid host "
            "pattern 'all:' supplied.\n"
            "\n"
            "[ERROR]: Completely failed to parse inventory source /scratch/20_bad.yml\n"
        )
        out = inventory_resolve._without_source_excerpt(stderr)
        assert "Failed to parse inventory with 'ini' plugin" in out
        assert "Completely failed to parse inventory source" in out
        assert out == stderr.rstrip("\n")

    def test_more_than_one_excerpt_is_removed(self):
        stderr = (
            "Origin: /scratch/a.yml:2:1\n"
            "\n"
            "1 first_leak: one\n"
            "2 x: !vault |\n"
            "  ^ column 1\n"
            "\n"
            "Origin: /scratch/b.yml:9:1\n"
            "\n"
            "8 second_leak: two\n"
            "9 y: !vault |\n"
        )
        out = inventory_resolve._without_source_excerpt(stderr)
        assert "first_leak" not in out and "second_leak" not in out
        assert out.count("Origin:") == 2

    @needs_vault
    def test_the_real_refusal_carries_no_source_lines(self, tmp_path, monkeypatch):
        """Driven, because the stripper's input is ansible's text and nobody
        else's -- a hand-written fixture asserts a shape rather than the shape."""
        root = _vaulted_tree(tmp_path)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(inventory_resolve, "ansible_inventory_binary", lambda: _ANSIBLE)

        with pytest.raises(inventory_resolve.InventoryResolutionFailed) as exc:
            asyncio.run(
                inventory_resolve.run_ansible_graph([root / "inv"], scratch=scratch, cwd=root)
            )
        message = str(exc.value)
        assert _NEIGHBOUR not in message, (
            "ansible's source excerpt reached the refusal, so the lines around the "
            f"vaulted value are in the API response: {message}"
        )
        assert "plain_var" not in message
        assert "web1.example.com.yml" in message, (
            "the file:line pointer went with the excerpt -- an operator now has nothing to open"
        )


class TestAttributingAVaultedVariable:
    """The two sources cannot print the same text for a vaulted value, so the
    comparison has to know that.

    Measured: `--list` emits the ciphertext mapping even when a vault password
    IS available, so this is the only shape it ever produces for a vaulted
    value rather than an edge case.
    """

    _CIPHER = {"__ansible_vault": "$ANSIBLE_VAULT;1.1;AES256\n3161\n"}

    def test_a_vaulted_group_variable_is_credited_to_the_group(self):
        out = attribute(
            node="web1",
            is_host=True,
            effective={"group_secret": self._CIPHER},
            groups_of_node=["web"],
            at_level={"@all": {}, "@web": {"group_secret": VAULTED_PLACEHOLDER}},
            children={},
        )
        assert out["group_secret"]["origin"] == "web", out
        assert out["group_secret"]["chain"] == [{"level": "web", "value": VAULTED_PLACEHOLDER}]

    def test_a_vaulted_host_variable_is_still_credited_to_the_host(self):
        """The host's own line shows its EFFECTIVE set, so nothing in the
        ancestry supplies it and `here` is the honest answer."""
        out = attribute(
            node="web1",
            is_host=True,
            effective={"db_password": self._CIPHER},
            groups_of_node=["web"],
            at_level={"@all": {}, "@web": {}},
            children={},
        )
        assert out["db_password"]["origin"] == "here"

    def test_a_host_override_of_a_vaulted_group_variable_still_wins(self):
        """Both levels print `<vaulted>`, and that is not a collision: the
        winner is the DEEPEST level carrying the key whatever the value, which
        is exactly what the deepest-match walk answers."""
        out = attribute(
            node="web",
            is_host=False,
            effective={"shared": self._CIPHER},
            groups_of_node=["web"],
            at_level={
                "@all": {"shared": VAULTED_PLACEHOLDER},
                "@web": {"shared": VAULTED_PLACEHOLDER},
            },
            children={"all": ["web"]},
        )
        assert out["shared"]["origin"] == "web", out

    def test_an_ordinary_value_is_unaffected(self):
        """The placeholder branch must not capture anything else: a plain
        mapping value is still compared as its own string."""
        out = attribute(
            node="web1",
            is_host=True,
            effective={"opts": {"a": 1}},
            groups_of_node=["web"],
            at_level={"@all": {}, "@web": {"opts": "{'a': 1}"}},
            children={},
        )
        assert out["opts"]["origin"] == "web", out

    def test_a_mapping_that_merely_mentions_the_marker_is_not_treated_as_vaulted(self):
        """The test is the SHAPE -- a one-key mapping, which is ansible's own
        wire form -- not the presence of the key, so an operator's own variable
        holding that name alongside others is compared normally."""
        value = {"__ansible_vault": "x", "note": "mine"}
        out = attribute(
            node="web1",
            is_host=True,
            effective={"thing": value},
            groups_of_node=["web"],
            at_level={"@all": {}, "@web": {"thing": str(value)}},
            children={},
        )
        assert out["thing"]["origin"] == "web", out
