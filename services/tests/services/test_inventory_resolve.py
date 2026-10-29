"""Resolving an inventory, driven against REAL ansible (#1967).

Terrapod implements no merge of its own, so every claim here is a claim about
ansible's behaviour -- which means a test that does not run ansible asserts
nothing about the thing under test. `docker/Dockerfile.test` installs
`ansible-core` into its own virtualenv for exactly this reason, so these run in
CI rather than skipping.

The two output shapes that are load-bearing were both FOUND BY RUNNING IT, and
neither is documented:

* **a host with no variables is absent from `_meta.hostvars` entirely** -- it
  appears only in a group's membership list, so enumerating the host set from
  that map loses it silently, which is the shape of bug that empties a target
  set;
* **a group with no members and no variables has no top-level entry** -- it
  appears only in `all.children`, so a group an operator has just declared would
  be missing from the view they declared it in.

Re-confirmed on ansible-core 2.21.5, which also added a `profile` key -- inside
`_meta` rather than at the top level, so it is never mistaken for a group.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from terrapod.services.inventory_resolve import (
    CACHE_TTL_SECONDS,
    InventorySourceRefused,
    _ansible_env,
    _screen_config_root,
    cache_key,
    normalise,
    render_inventory_yaml,
)


def _working_ansible() -> str | None:
    """A usable `ansible-inventory`, or None.

    **Probed, not merely located.** `shutil.which` finding a file says nothing
    about it working: this machine carries a leftover
    `~/Library/Python/3.13/bin/ansible-inventory` whose `ansible` module is long
    gone, so the located-only version of this check ran eleven tests against a
    shim that dies with `ModuleNotFoundError` -- failures that look like the
    code under test and are not.

    Skipping rather than failing on a broken one is right HERE and nowhere else:
    a stale shim on a developer's machine must not fail the suite, and the test
    image's own install is gated at BUILD time by `ansible-inventory --version`
    in `docker/Dockerfile.test`. So a broken install in CI fails the image
    build, where it belongs, rather than quietly skipping these.
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


_ANSIBLE = _working_ansible()
needs_ansible = pytest.mark.skipif(
    _ANSIBLE is None,
    reason=(
        "no WORKING ansible-inventory on PATH. The test image installs one "
        "(docker/Dockerfile.test) and verifies it at build time, so CI runs these."
    ),
)


def _run(inventory: Path, *, scratch: Path, limit: str | None = None) -> dict:
    """`ansible-inventory --list` under **production's own environment**.

    `_ansible_env` rather than a dict assembled here (#2035). These tests exist
    to say what real ansible does with what the service feeds it, and a harness
    that builds its own environment answers that question about a configuration
    production does not use -- which is how the old version came to pass
    `ANSIBLE_CONFIG` that the service no longer sets at all.
    """
    argv = [_ANSIBLE, "--list", "-i", str(inventory)]
    if limit:
        argv += ["--limit", limit]
    proc = subprocess.run(  # noqa: S603 -- argv is built here, not from input
        argv,
        capture_output=True,
        env=_ansible_env(scratch),
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """The service's scratch directory -- what `ANSIBLE_HOME` hangs off."""
    return tmp_path


class TestTheRenderedDocumentIsValidInventory:
    """What ansible makes of what we write. The renderer's only real contract."""

    @needs_ansible
    def test_a_host_with_no_variables_survives_the_round_trip(self, tmp_path, scratch):
        """The omission that loses hosts. `switch-1` has no variables, so
        ansible's `_meta.hostvars` will not mention it at all."""
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("switch-1", {}), ("web-1", {"ansible_host": "10.0.0.4"})],
                groups=[("net", {})],
                memberships=[("net", "switch-1")],
                children=[],
                global_vars={},
            )
        )

        raw = _run(document, scratch=scratch)
        # The trap, asserted on ansible's own output rather than assumed.
        assert "switch-1" not in (raw["_meta"]["hostvars"]), (
            "ansible started reporting var-less hosts in _meta.hostvars; the union in "
            "normalise() is now belt-and-braces rather than load-bearing, which is worth "
            "knowing before anyone simplifies it"
        )

        resolved = normalise(raw)
        assert sorted(resolved["hosts"]) == ["switch-1", "web-1"]
        assert resolved["groups"] == {"net": ["switch-1"]}

    @needs_ansible
    def test_a_group_with_no_members_survives(self, tmp_path, scratch):
        """The other omission. An operator who declares a group and sees it
        missing from the resolved view reads that as the write having failed."""
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {})],
                groups=[("web", {}), ("empty", {})],
                memberships=[("web", "web-1")],
                children=[],
                global_vars={},
            )
        )

        raw = _run(document, scratch=scratch)
        assert "empty" not in raw, "ansible started emitting a top-level entry for it"
        assert "empty" in (raw["all"]["children"])

        resolved = normalise(raw)
        assert resolved["groups"]["empty"] == []
        assert resolved["groups"]["web"] == ["web-1"]

    @needs_ansible
    def test_group_variables_reach_their_members_and_nobody_else(self, tmp_path, scratch):
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {}), ("db-1", {})],
                groups=[("web", {"http_port": 80}), ("db", {})],
                memberships=[("web", "web-1"), ("db", "db-1")],
                children=[],
                global_vars={},
            )
        )
        hostvars = _run(document, scratch=scratch)["_meta"]["hostvars"]
        assert hostvars["web-1"]["http_port"] == 80
        assert "http_port" not in hostvars.get("db-1", {})

    @needs_ansible
    def test_a_host_variable_beats_a_group_variable(self, tmp_path, scratch):
        """Ansible's precedence, not ours -- asserted so the rendered shape is
        known to express it rather than assumed to."""
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {"http_port": 8080})],
                groups=[("web", {"http_port": 80})],
                memberships=[("web", "web-1")],
                children=[],
                global_vars={},
            )
        )
        hostvars = _run(document, scratch=scratch)["_meta"]["hostvars"]
        assert hostvars["web-1"]["http_port"] == 8080

    @needs_ansible
    def test_inventory_wide_variables_reach_every_host(self, tmp_path, scratch):
        """`group_vars/all`, rendered at the document's root.

        This is what `all` being refused as a group NAME buys: the root is where
        these go, and a declared group called `all` would collide with it.
        """
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {}), ("lonely", {})],
                groups=[("web", {})],
                memberships=[("web", "web-1")],
                children=[],
                global_vars={"ansible_python_interpreter": "/usr/bin/python3"},
            )
        )
        hostvars = _run(document, scratch=scratch)["_meta"]["hostvars"]
        for host in ("web-1", "lonely"):
            assert hostvars[host]["ansible_python_interpreter"] == "/usr/bin/python3", (
                f"{host} did not inherit the inventory-wide variable"
            )

    @needs_ansible
    def test_a_more_specific_group_beats_a_less_specific_one(self, tmp_path, scratch):
        """Parent before child, which is ansible's rule and the reason the
        nesting is emitted as `children` rather than flattened."""
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {})],
                groups=[("prod", {"tier": "parent"}), ("web", {"tier": "child"})],
                memberships=[("web", "web-1")],
                children=[("prod", "web")],
                global_vars={},
            )
        )
        hostvars = _run(document, scratch=scratch)["_meta"]["hostvars"]
        assert hostvars["web-1"]["tier"] == "child"

    @needs_ansible
    def test_nesting_is_STRUCTURAL_and_not_flattened_into_the_parent(self, tmp_path, scratch):
        """Found by running it, having assumed the opposite.

        A group's `hosts` is DIRECT membership. A parent whose members all come
        through a child reports no `hosts` of its own, so a view built from
        `groups` alone would say `prod` is empty while a configure targeting
        `prod` would reach `web-1`. Hence `children` is carried through.
        """
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {})],
                groups=[("prod", {}), ("web", {})],
                memberships=[("web", "web-1")],
                children=[("prod", "web")],
                global_vars={},
            )
        )
        resolved = normalise(_run(document, scratch=scratch))

        assert resolved["groups"]["prod"] == [], "ansible started flattening nesting"
        assert resolved["groups"]["web"] == ["web-1"]
        # The structure, without which the parent's reach is invisible.
        assert resolved["children"]["prod"] == ["web"]

    @needs_ansible
    def test_a_limit_on_a_parent_reaches_its_children_s_hosts(self, tmp_path, scratch):
        """The other half, and the one that matters for targeting.

        Ansible expands nesting for `--limit`, so the effective set of a parent
        IS answerable -- just not from `groups`. This is why the limit is a
        parameter on the resolved read rather than something a consumer
        assembles from the structure itself.
        """
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {}), ("db-1", {})],
                groups=[("prod", {}), ("web", {}), ("dbs", {})],
                memberships=[("web", "web-1"), ("dbs", "db-1")],
                children=[("prod", "web")],
                global_vars={},
            )
        )

        everything = normalise(_run(document, scratch=scratch))
        assert sorted(everything["hosts"]) == ["db-1", "web-1"]

        # `prod` holds no host directly, and limiting to it still selects the
        # one its child holds -- and only that one.
        narrowed = normalise(_run(document, scratch=scratch, limit="prod"))
        assert sorted(narrowed["hosts"]) == ["web-1"]

    @needs_ansible
    def test_a_regex_limit_is_EXPANDED_rather_than_refused(self, tmp_path, scratch):
        """The behaviour change this whole module exists for.

        Terrapod's own expander refused a `~regex` term, because quietly
        matching nothing would have been the wrong answer dressed as an answer.
        Ansible expands it, so there is nothing left to refuse -- and that is
        the point of not reimplementing the pattern language.
        """
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {}), ("web-2", {}), ("db-1", {})],
                groups=[],
                memberships=[],
                children=[],
                global_vars={},
            )
        )
        resolved = normalise(_run(document, scratch=scratch, limit="~web.*"))
        assert sorted(resolved["hosts"]) == ["web-1", "web-2"]

    @needs_ansible
    def test_an_exclusion_limit_expands(self, tmp_path, scratch):
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(
                hosts=[("web-1", {}), ("web-2", {})],
                groups=[("web", {})],
                memberships=[("web", "web-1"), ("web", "web-2")],
                children=[],
                global_vars={},
            )
        )
        resolved = normalise(_run(document, scratch=scratch, limit="web:!web-2"))
        assert sorted(resolved["hosts"]) == ["web-1"]

    @needs_ansible
    def test_an_empty_inventory_is_an_empty_result_not_an_error(self, tmp_path, scratch):
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(hosts=[], groups=[], memberships=[], children=[], global_vars={})
        )
        resolved = normalise(_run(document, scratch=scratch))
        assert resolved == {"hosts": {}, "groups": {}, "children": {}}


class TestTheRenderedYamlShape:
    """Pure assertions, so they run everywhere. The round-trip above is what
    proves the shape MEANS anything; these pin the structure itself."""

    def test_it_is_rooted_at_all(self):
        """Which is why `all` cannot be a declared group name."""
        document = yaml.safe_load(
            render_inventory_yaml(
                hosts=[("h", {})], groups=[], memberships=[], children=[], global_vars={}
            )
        )
        assert list(document) == ["all"]

    def test_every_host_is_at_the_root_even_when_grouped(self):
        """So a group-less host is still in the inventory, rather than existing
        only if some group happens to name it."""
        document = yaml.safe_load(
            render_inventory_yaml(
                hosts=[("grouped", {}), ("lonely", {})],
                groups=[("g", {})],
                memberships=[("g", "grouped")],
                children=[],
                global_vars={},
            )
        )
        assert sorted(document["all"]["hosts"]) == ["grouped", "lonely"]

    def test_the_same_rows_render_the_same_bytes(self):
        """Sorted output, so two reads are comparable when something looks
        wrong -- and so a diff between them is readable."""
        args = {
            "hosts": [("b", {"x": 1}), ("a", {})],
            "groups": [("z", {}), ("y", {})],
            "memberships": [("z", "b"), ("y", "a")],
            "children": [("z", "y")],
            "global_vars": {"k": "v"},
        }
        assert render_inventory_yaml(**args) == render_inventory_yaml(**args)


class TestThePluginRefusal:
    """Layer 3 of the dynamic-inventory ban (#1970, closed NOT_PLANNED).

    Legibility rather than enforcement: measured against ansible-core 2.18.3,
    its own `yaml` plugin recognises a plugin configuration and refuses it, and
    the forced `ANY_UNPARSED_IS_FAILED` promotes that to a failed resolution. So
    ansible already fails closed; this layer exists because ansible names the
    file and not what to do about it.

    `allow_plugins` is the per-workspace switch (#2033), and what it changes is
    NOT the refusal -- it is that the file leaves the tree. Both directions are
    driven here, because the switch is only meaningful as the difference.
    """

    def test_a_plugin_config_is_refused_by_name(self, tmp_path):
        source = tmp_path / "20_cloud.aws_ec2.yml"
        source.write_text("plugin: amazon.aws.aws_ec2\nregions:\n  - eu-west-1\n")

        with pytest.raises(InventorySourceRefused) as exc:
            _screen_config_root(tmp_path, allow_plugins=False)

        message = str(exc.value)
        assert "20_cloud.aws_ec2.yml" in message, "the refusal must name the file"
        assert "amazon.aws.aws_ec2" in message, "and the plugin it asks for"
        # And where to go instead, or the refusal just moves the dead end.
        assert "data source" in message and "for_each" in message

    def test_with_the_switch_on_it_is_skipped_and_removed(self, tmp_path):
        """Skipped means EXCLUDED, not "parsed anyway" (#2033).

        Measured: with the plugin config present, resolution is rc=1 under
        `yaml,ini` AND under `auto,yaml,ini`, because the collection is not
        installed here either way. Only removing it from the tree resolves. So
        the switch cannot mean "stop refusing"; the file has to go.
        """
        source = tmp_path / "20_cloud.aws_ec2.yml"
        source.write_text("plugin: amazon.aws.aws_ec2\n")

        skipped, _executables = _screen_config_root(tmp_path, allow_plugins=True)

        assert not source.exists(), (
            "the plugin config is still in the tree, so this resolution will fail "
            "rather than skip -- the whole mechanism is exclusion"
        )
        assert [(s.kind, s.plugin) for s in skipped] == [("plugin", "amazon.aws.aws_ec2")]
        assert "20_cloud.aws_ec2.yml" in skipped[0].path
        assert skipped[0].reason, "a skip with no reason is an unlabelled short host set"

    def test_a_static_inventory_beside_it_is_untouched(self, tmp_path):
        (tmp_path / "10_static.yml").write_text("all:\n  hosts:\n    web-1:\n")
        skipped, _ = _screen_config_root(tmp_path, allow_plugins=False)  # must not raise
        assert skipped == []

    def test_the_exec_bit_comes_off_everything(self, tmp_path):
        """#1970: a static `hosts.yml` beside an executable `sneaky.sh` resolved
        to the union of both -- "the exec bit is the whole decision"."""
        script = tmp_path / "sneaky.sh"
        script.write_text("#!/bin/sh\necho '{}'\n")
        script.chmod(0o755)
        assert script.stat().st_mode & stat.S_IXUSR

        _skipped, executables = _screen_config_root(tmp_path, allow_plugins=False)

        mode = script.stat().st_mode
        assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        assert script in executables, (
            "the bit is stripped here and the source screening runs later, so a file "
            "that WAS executable is unrecoverable unless it is recorded now"
        )

    def test_the_exec_bit_comes_off_even_with_scripts_enabled(self, tmp_path):
        """The switch is the RUNNER's; the API strips regardless (#2033).

        `_screen_config_root` takes no `allow_scripts`, and that is the property:
        the API never runs an inventory script whatever the workspace says,
        because the objection was always about where the code runs -- and the
        API pod's environment holds the database URL and the KEK.
        """
        script = tmp_path / "hosts.sh"
        script.write_text("#!/bin/sh\necho '{}'\n")
        script.chmod(0o755)

        _screen_config_root(tmp_path, allow_plugins=True)

        assert not script.stat().st_mode & stat.S_IXUSR

    def test_a_nested_plugin_config_is_found_too(self, tmp_path):
        """A directory is read recursively, so the walk has to be."""
        nested = tmp_path / "hosts.d" / "cloud"
        nested.mkdir(parents=True)
        (nested / "ec2.yml").write_text("plugin: amazon.aws.aws_ec2\n")
        with pytest.raises(InventorySourceRefused):
            _screen_config_root(tmp_path, allow_plugins=False)

    def test_a_yaml_file_that_is_not_a_mapping_is_left_alone(self, tmp_path):
        """An INI inventory, a list, a stray document -- not our business to
        refuse, and ansible reports what it makes of them."""
        (tmp_path / "list.yml").write_text("- one\n- two\n")
        (tmp_path / "junk.yml").write_text(": : :\n")
        skipped, _ = _screen_config_root(tmp_path, allow_plugins=False)  # must not raise
        assert skipped == []


class TestTheResolutionEnvironment:
    """Our settings reach ansible, and nothing else does (#2035).

    These replace two tests that asserted the TEXT of a generated `ansible.cfg`.
    The properties survive; the mechanism is the environment, because
    `ansible.cfg` is single-file with no merging and writing one DISCARDED the
    operator's own -- including the `inventory_ignore_extensions` that decides
    which files in a directory are sources, so the API resolved a different
    source set from the runner for a reason nothing to do with the version.
    """

    def test_it_enables_only_the_static_plugins(self, tmp_path):
        """Layer 2: no `auto` (the dispatcher that runs whatever a file's
        `plugin:` key names) and no `script` (an executable source)."""
        enabled = {
            p.strip() for p in _ansible_env(tmp_path)["ANSIBLE_INVENTORY_ENABLED"].split(",")
        }
        assert enabled == {"yaml", "ini"}
        assert "auto" not in enabled and "script" not in enabled

    def test_an_unparseable_source_fails_the_resolution(self, tmp_path):
        """`any_unparsed_is_failed`, adopted from a production repository. It is
        #1967's requirement exactly: a source that cannot be read must not
        silently resolve to a partial host set."""
        assert _ansible_env(tmp_path)["ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED"] == "True"

    def test_it_sets_no_ansible_config_so_the_operators_own_is_read(self, tmp_path):
        """The whole point. `ANSIBLE_CONFIG` pointing anywhere means the
        repository's own `ansible.cfg` is not read at all -- measured against
        ansible-core 2.21.5, their `roles_path` and `inventory_ignore_extensions`
        disappeared from `ansible-config dump` entirely.
        """
        assert "ANSIBLE_CONFIG" not in _ansible_env(tmp_path)

    def test_an_inherited_ansible_setting_cannot_reach_ansible(self, tmp_path, monkeypatch):
        """Inherited `ANSIBLE_*` is removed, not merely overridden.

        Env beats `ansible.cfg`, so this is load-bearing rather than tidy: an
        inherited `ANSIBLE_CONFIG` would redirect the parse away from the
        repository's config -- reintroducing the defect by the back door the
        moment we stopped setting the key ourselves -- and an inherited
        `ANSIBLE_INVENTORY_ENABLED=auto` would re-enable the plugin dispatcher
        that layer 2 exists to disable.
        """
        monkeypatch.setenv("ANSIBLE_CONFIG", "/tmp/somebody-elses.cfg")
        monkeypatch.setenv("ANSIBLE_INVENTORY_ENABLED", "auto")
        monkeypatch.setenv("ANSIBLE_VAULT_PASSWORD_FILE", "/tmp/pw")
        monkeypatch.setenv("ANSIBLE_INVENTORY_IGNORE", ".yml")

        env = _ansible_env(tmp_path)
        assert "ANSIBLE_CONFIG" not in env
        assert "ANSIBLE_VAULT_PASSWORD_FILE" not in env
        assert "ANSIBLE_INVENTORY_IGNORE" not in env, (
            "an inherited ignore list would silently change which files are sources"
        )
        assert env["ANSIBLE_INVENTORY_ENABLED"] == "yaml,ini"

    @needs_ansible
    def test_the_unparsed_setting_really_bites(self, tmp_path, scratch):
        """Driven, because the setting existing says nothing about it working.

        A directory holding one good source and one ansible cannot parse must
        fail rather than resolve to the good half.
        """
        sources = tmp_path / "hosts.d"
        sources.mkdir()
        (sources / "10_good.yml").write_text("all:\n  hosts:\n    web-1:\n")
        (sources / "20_bad.yml").write_text("all:\n  hosts:\n   - this is not a mapping\n")

        proc = subprocess.run(  # noqa: S603
            [_ANSIBLE, "--list", "-i", str(sources)],
            capture_output=True,
            env=_ansible_env(scratch),
            check=False,
        )
        assert proc.returncode != 0, (
            "an unparseable source resolved to the good half instead of failing; "
            f"any_unparsed_is_failed is not in effect. output: {proc.stdout[:400]!r}"
        )
        # A non-zero exit ALONE would also be satisfied by an ansible that is
        # merely broken, and `_working_ansible` cannot rule that out: its probe
        # is `--version`, which ansible answers BEFORE importing its own module,
        # so a stale shim whose `ansible` package is gone passes the probe and
        # then fails every real invocation. Without this the test would go green
        # on a developer machine for a reason that has nothing to do with the
        # setting it exists to prove.
        err = proc.stderr.decode(errors="replace")
        assert "20_bad.yml" in err or "20_bad.yml" in proc.stdout.decode(errors="replace"), (
            "it failed, but not demonstrably because of the unparseable source -- "
            f"so this may be a broken ansible rather than the setting biting. stderr: {err[:400]!r}"
        )


class TestTheCacheKey:
    """Content-addressed, so a write supersedes an entry rather than needing an
    invalidation in every one of the seven structures' writers."""

    def test_the_limit_is_hashed_rather_than_embedded(self):
        """It is operator text, and a colon is the key separator -- so
        `web:!web-2` would otherwise put three segments in the key."""
        ws = uuid.uuid4()
        key = cache_key(ws, "rev", "sha", "web:!web-2")
        assert "web" not in key
        assert key.startswith(f"tp:inv_resolved:{ws}:rev:sha:")

    def test_an_absent_limit_is_a_different_key_from_any_limit(self):
        ws = uuid.uuid4()
        assert cache_key(ws, "r", "s", None) != cache_key(ws, "r", "s", "all")

    def test_two_different_limits_do_not_share_an_entry(self):
        ws = uuid.uuid4()
        assert cache_key(ws, "r", "s", "web") != cache_key(ws, "r", "s", "db")

    def test_a_missing_git_sha_is_still_a_well_formed_key(self):
        """The common case: an inventory with no repository bound."""
        key = cache_key(uuid.uuid4(), "rev", "", None)
        assert key.endswith(":rev:-")

    def test_the_ttl_is_bounded(self):
        """Only because of the git source -- a derived `platform_rev` cannot be
        stale, but a git sha is only knowable by fetching."""
        assert 0 < CACHE_TTL_SECONDS <= 3600


class TestEveryMaterialisedSourceIsScanned:
    """A positional gate, because no behavioural test can see this.

    The plugin refusal has to be CALLED by the path that materialises a source.
    A test that drives `_refuse_plugin_sources` directly passes whether or not
    anything invokes it -- which is exactly how a sibling treatment on this same
    path, an ignore-paths pruner removed in #2256, came to be stored and ignored.

    **It reads `_materialise_config_root`, and that covers BOTH sources** (#2021).
    The treatment used to live inside `_fetch_vcs_source`, so this gate spoke for
    the fetch path alone; the run path reads the configuration version instead,
    and a second copy of the chmod and the plugin refusal is exactly how one of
    the two comes to be missing it. One function, one gate, both paths.
    """

    def _calls(self) -> list[tuple[str, str]]:
        """Every call in `_materialise_config_root`, as (callee, unparsed args).

        Read from the AST rather than by searching the text, which matters more
        than it looks: `_screen_config_root` is NAMED IN THE DOCSTRING above the
        code, so a substring search finds the prose first. An earlier version of
        this gate did exactly that and failed against correct code.
        """
        import ast
        import inspect
        import textwrap

        from terrapod.services import inventory_resolve

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(inventory_resolve._materialise_config_root))
        )
        out: list[tuple[str, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                out.append((ast.unparse(node.func), " ".join(ast.unparse(a) for a in node.args)))
        return out

    def test_the_plugin_scan_is_called_on_the_materialised_root(self):
        scans = [
            args
            for callee, args in self._calls()
            if callee.endswith("to_thread") and "_screen_config_root" in args
        ]
        assert scans, (
            "`_materialise_config_root` does not call `_screen_config_root`, so a "
            "fetched repository reaches ansible unscanned: a plugin config in it is "
            "no longer named and refused, and no exec bit is stripped"
        )
