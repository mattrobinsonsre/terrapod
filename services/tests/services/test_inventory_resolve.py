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
import os
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
    _apply_ignore_paths,
    _refuse_plugin_sources,
    _write_ansible_cfg,
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


def _run(inventory: Path, *, cfg: Path, limit: str | None = None) -> dict:
    """`ansible-inventory --list`, the way the service runs it."""
    argv = [_ANSIBLE, "--list", "-i", str(inventory)]
    if limit:
        argv += ["--limit", limit]
    proc = subprocess.run(  # noqa: S603 -- argv is built here, not from input
        argv,
        capture_output=True,
        # ANSIBLE_HOME rather than HOME -- see the note in
        # `inventory_resolve.run_ansible_inventory`: moving HOME moves where
        # the interpreter finds user site-packages, so it can make the very
        # tool under test unimportable.
        env={
            **os.environ,
            "ANSIBLE_CONFIG": str(cfg),
            "ANSIBLE_HOME": str(cfg.parent / ".ansible"),
        },
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    return json.loads(proc.stdout)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return _write_ansible_cfg(tmp_path)


class TestTheRenderedDocumentIsValidInventory:
    """What ansible makes of what we write. The renderer's only real contract."""

    @needs_ansible
    def test_a_host_with_no_variables_survives_the_round_trip(self, tmp_path, cfg):
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

        raw = _run(document, cfg=cfg)
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
    def test_a_group_with_no_members_survives(self, tmp_path, cfg):
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

        raw = _run(document, cfg=cfg)
        assert "empty" not in raw, "ansible started emitting a top-level entry for it"
        assert "empty" in (raw["all"]["children"])

        resolved = normalise(raw)
        assert resolved["groups"]["empty"] == []
        assert resolved["groups"]["web"] == ["web-1"]

    @needs_ansible
    def test_group_variables_reach_their_members_and_nobody_else(self, tmp_path, cfg):
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
        hostvars = _run(document, cfg=cfg)["_meta"]["hostvars"]
        assert hostvars["web-1"]["http_port"] == 80
        assert "http_port" not in hostvars.get("db-1", {})

    @needs_ansible
    def test_a_host_variable_beats_a_group_variable(self, tmp_path, cfg):
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
        hostvars = _run(document, cfg=cfg)["_meta"]["hostvars"]
        assert hostvars["web-1"]["http_port"] == 8080

    @needs_ansible
    def test_inventory_wide_variables_reach_every_host(self, tmp_path, cfg):
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
        hostvars = _run(document, cfg=cfg)["_meta"]["hostvars"]
        for host in ("web-1", "lonely"):
            assert hostvars[host]["ansible_python_interpreter"] == "/usr/bin/python3", (
                f"{host} did not inherit the inventory-wide variable"
            )

    @needs_ansible
    def test_a_more_specific_group_beats_a_less_specific_one(self, tmp_path, cfg):
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
        hostvars = _run(document, cfg=cfg)["_meta"]["hostvars"]
        assert hostvars["web-1"]["tier"] == "child"

    @needs_ansible
    def test_nesting_is_STRUCTURAL_and_not_flattened_into_the_parent(self, tmp_path, cfg):
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
        resolved = normalise(_run(document, cfg=cfg))

        assert resolved["groups"]["prod"] == [], "ansible started flattening nesting"
        assert resolved["groups"]["web"] == ["web-1"]
        # The structure, without which the parent's reach is invisible.
        assert resolved["children"]["prod"] == ["web"]

    @needs_ansible
    def test_a_limit_on_a_parent_reaches_its_children_s_hosts(self, tmp_path, cfg):
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

        everything = normalise(_run(document, cfg=cfg))
        assert sorted(everything["hosts"]) == ["db-1", "web-1"]

        # `prod` holds no host directly, and limiting to it still selects the
        # one its child holds -- and only that one.
        narrowed = normalise(_run(document, cfg=cfg, limit="prod"))
        assert sorted(narrowed["hosts"]) == ["web-1"]

    @needs_ansible
    def test_a_regex_limit_is_EXPANDED_rather_than_refused(self, tmp_path, cfg):
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
        resolved = normalise(_run(document, cfg=cfg, limit="~web.*"))
        assert sorted(resolved["hosts"]) == ["web-1", "web-2"]

    @needs_ansible
    def test_an_exclusion_limit_expands(self, tmp_path, cfg):
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
        resolved = normalise(_run(document, cfg=cfg, limit="web:!web-2"))
        assert sorted(resolved["hosts"]) == ["web-1"]

    @needs_ansible
    def test_an_empty_inventory_is_an_empty_result_not_an_error(self, tmp_path, cfg):
        document = tmp_path / "platform.yml"
        document.write_text(
            render_inventory_yaml(hosts=[], groups=[], memberships=[], children=[], global_vars={})
        )
        resolved = normalise(_run(document, cfg=cfg))
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

    Disabling a plugin does not make its file inert -- it makes it GARBAGE
    INPUT. A `*.aws_ec2.yml` ends in `.yml`, so the `yaml` plugin claims it
    whatever is inside and then reads `plugin:`, `regions:` and `filters:` as
    group names. An operator would get a confusing parse rather than an answer.
    """

    def test_a_plugin_config_is_refused_by_name(self, tmp_path):
        source = tmp_path / "20_cloud.aws_ec2.yml"
        source.write_text("plugin: amazon.aws.aws_ec2\nregions:\n  - eu-west-1\n")

        with pytest.raises(InventorySourceRefused) as exc:
            _refuse_plugin_sources(tmp_path)

        message = str(exc.value)
        assert "20_cloud.aws_ec2.yml" in message, "the refusal must name the file"
        assert "amazon.aws.aws_ec2" in message, "and the plugin it asks for"
        # And where to go instead, or the refusal just moves the dead end.
        assert "data source" in message and "for_each" in message

    def test_a_static_inventory_beside_it_is_untouched(self, tmp_path):
        (tmp_path / "10_static.yml").write_text("all:\n  hosts:\n    web-1:\n")
        _refuse_plugin_sources(tmp_path)  # must not raise

    def test_the_exec_bit_comes_off_everything(self, tmp_path):
        """#1970: a static `hosts.yml` beside an executable `sneaky.sh` resolved
        to the union of both -- "the exec bit is the whole decision"."""
        script = tmp_path / "sneaky.sh"
        script.write_text("#!/bin/sh\necho '{}'\n")
        script.chmod(0o755)
        assert script.stat().st_mode & stat.S_IXUSR

        _refuse_plugin_sources(tmp_path)

        mode = script.stat().st_mode
        assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def test_a_nested_plugin_config_is_found_too(self, tmp_path):
        """A directory is read recursively, so the walk has to be."""
        nested = tmp_path / "hosts.d" / "cloud"
        nested.mkdir(parents=True)
        (nested / "ec2.yml").write_text("plugin: amazon.aws.aws_ec2\n")
        with pytest.raises(InventorySourceRefused):
            _refuse_plugin_sources(tmp_path)

    def test_a_yaml_file_that_is_not_a_mapping_is_left_alone(self, tmp_path):
        """An INI inventory, a list, a stray document -- not our business to
        refuse, and ansible reports what it makes of them."""
        (tmp_path / "list.yml").write_text("- one\n- two\n")
        (tmp_path / "junk.yml").write_text(": : :\n")
        _refuse_plugin_sources(tmp_path)  # must not raise


class TestTheGeneratedAnsibleConfig:
    def test_it_enables_only_the_static_plugins(self, tmp_path):
        """Layer 2: no `auto` (the dispatcher that runs whatever a file's
        `plugin:` key names) and no `script` (an executable source)."""
        text = _write_ansible_cfg(tmp_path).read_text()
        line = next(ln for ln in text.splitlines() if ln.startswith("enable_plugins"))
        enabled = {p.strip() for p in line.split("=", 1)[1].split(",")}
        assert enabled == {"yaml", "ini"}
        assert "auto" not in enabled and "script" not in enabled

    def test_an_unparseable_source_fails_the_resolution(self, tmp_path):
        """`any_unparsed_is_failed`, adopted from a production repository. It is
        #1967's requirement exactly: a source that cannot be read must not
        silently resolve to a partial host set."""
        text = _write_ansible_cfg(tmp_path).read_text()
        assert "any_unparsed_is_failed = True" in text

    @needs_ansible
    def test_the_unparsed_setting_really_bites(self, tmp_path, cfg):
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
            env={
                **os.environ,
                "ANSIBLE_CONFIG": str(cfg),
                "ANSIBLE_HOME": str(tmp_path / ".ansible"),
            },
            check=False,
        )
        assert proc.returncode != 0, (
            "an unparseable source resolved to the good half instead of failing; "
            f"any_unparsed_is_failed is not in effect. output: {proc.stdout[:400]!r}"
        )


class TestTheCacheKey:
    """Content-addressed, so a write supersedes an entry rather than needing an
    invalidation in every one of the eight structures' writers."""

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


class TestIgnorePathsIsActuallyApplied:
    """`ignore_paths` was stored, serialised and honoured by NOTHING (#1967).

    It was validated on the way in, persisted, returned by the API and carried
    by replication -- and the resolution never looked at it. That is the
    "a flag exists and something must read it" shape this project has shipped
    before (the OCI `enabled` flag that disabled two scheduled tasks while the
    registry happily served push and pull), and here it failed in the direction
    that reads as harmless: the source stayed WIDER than the operator asked
    for, so hosts they had excluded appeared.

    Ansible reads a directory as one source and takes every file in it, so
    pruning the tree before ansible sees it is the only way to leave a file out.
    """

    def test_a_named_directory_and_its_contents_are_removed(self, tmp_path: Path):
        (tmp_path / "hosts.yml").write_text("all:\n")
        (tmp_path / "archive").mkdir()
        (tmp_path / "archive" / "retired.yml").write_text("all:\n")

        _apply_ignore_paths(tmp_path, ["archive"])

        assert (tmp_path / "hosts.yml").exists()
        assert not (tmp_path / "archive").exists()

    def test_a_glob_matches_the_same_way_an_autodiscovery_rule_does(self, tmp_path: Path):
        """Same matcher, so the two settings mean the same thing to an
        operator who has written one of them before."""
        (tmp_path / "hosts.yml").write_text("all:\n")
        (tmp_path / "README.txt").write_text("notes\n")
        (tmp_path / "staging.ini").write_text("[web]\n")

        _apply_ignore_paths(tmp_path, ["*.txt", "staging.ini"])

        assert (tmp_path / "hosts.yml").exists()
        assert not (tmp_path / "README.txt").exists()
        assert not (tmp_path / "staging.ini").exists()

    def test_patterns_are_relative_to_the_working_directory(self, tmp_path: Path):
        """`root` is already narrowed by `working_directory`, so a pattern is
        written against that -- an operator never prefixes a path they did not
        choose."""
        (tmp_path / "group_vars").mkdir()
        (tmp_path / "group_vars" / "all.yml").write_text("k: v\n")
        (tmp_path / "group_vars" / "secret.yml").write_text("k: v\n")

        _apply_ignore_paths(tmp_path, ["group_vars/secret.yml"])

        assert (tmp_path / "group_vars" / "all.yml").exists()
        assert not (tmp_path / "group_vars" / "secret.yml").exists()

    def test_an_empty_or_blank_list_removes_nothing(self, tmp_path: Path):
        """The default, and the overwhelmingly common case. A pattern list of
        blanks must not become a pattern that matches everything."""
        (tmp_path / "hosts.yml").write_text("all:\n")

        for patterns in ([], ["", "  "]):
            _apply_ignore_paths(tmp_path, patterns)
            assert (tmp_path / "hosts.yml").exists(), patterns

    def test_a_pattern_matching_nothing_leaves_the_tree_alone(self, tmp_path: Path):
        (tmp_path / "hosts.yml").write_text("all:\n")

        _apply_ignore_paths(tmp_path, ["does-not-exist/**"])

        assert (tmp_path / "hosts.yml").exists()

    @pytest.mark.skipif(_working_ansible() is None, reason="needs ansible-inventory")
    def test_an_ignored_source_contributes_no_hosts_to_a_real_resolution(self, tmp_path: Path):
        """The property, driven through ansible rather than asserted about the
        filesystem: a host in an ignored file must not reach the inventory.

        Asserting only that the file is gone would pass on a pruner that ran
        after ansible had already read the directory.
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "keep.yml").write_text("all:\n  hosts:\n    kept-01:\n")
        (src / "drop.yml").write_text("all:\n  hosts:\n    dropped-01:\n")

        _apply_ignore_paths(src, ["drop.yml"])

        cfg = _write_ansible_cfg(tmp_path)
        out = subprocess.run(  # noqa: S603
            [_working_ansible(), "--list", "-i", str(src)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "ANSIBLE_CONFIG": str(cfg), "ANSIBLE_HOME": str(tmp_path)},
        )
        hosts = normalise(json.loads(out.stdout))["hosts"]
        assert "kept-01" in hosts
        assert "dropped-01" not in hosts


class TestTheFetchPathReadsIgnorePaths:
    """A positional gate, because no behavioural test can see this.

    The defect was not a wrong pruner -- it was a pruner that was never called.
    A test that drives `_apply_ignore_paths` directly passes whether or not the
    fetch path invokes it, which is precisely how `ignore_paths` came to be
    stored and ignored. So this reads the source: the call must exist, must be
    passed the settings row's own list, and must come BEFORE the plugin scan,
    since scanning a file the operator excluded would refuse the resolution
    over something already out of the source.
    """

    def _calls(self) -> list[tuple[str, str]]:
        """Every call in `_fetch_vcs_source`, as (callee, unparsed arguments).

        Read from the AST rather than by searching the text, which matters more
        than it looks: `_refuse_plugin_sources` is NAMED IN THE DOCSTRING above
        the code, so a substring search finds the prose first and reports the
        order backwards. The first version of this gate did exactly that and
        failed against correct code.
        """
        import ast
        import inspect
        import textwrap

        from terrapod.services import inventory_resolve

        tree = ast.parse(textwrap.dedent(inspect.getsource(inventory_resolve._fetch_vcs_source)))
        out: list[tuple[str, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                out.append((ast.unparse(node.func), " ".join(ast.unparse(a) for a in node.args)))
        return out

    def test_the_fetch_passes_the_settings_rows_own_ignore_paths(self):
        calls = self._calls()
        pruning = [
            args for callee, args in calls if "_apply_ignore_paths" in (callee, *args.split())
        ]
        # The pruner is handed to `asyncio.to_thread`, so it is an ARGUMENT of
        # that call rather than the callee -- which is also how the real
        # rule-13 pattern looks everywhere else in this module.
        threaded = [
            args
            for callee, args in calls
            if callee.endswith("to_thread") and "_apply_ignore_paths" in args
        ]
        assert pruning or threaded, (
            "the fetch path does not prune the source, so `ignore_paths` is "
            "stored and honoured by nothing"
        )
        assert any("settings_row.ignore_paths" in args for args in (*pruning, *threaded)), (
            "the pruner is called with something other than the settings row's "
            "own list, so what an operator configured is not what is applied"
        )

    def test_the_prune_comes_before_the_plugin_scan(self):
        order = [
            name
            for callee, args in self._calls()
            for name in ("_apply_ignore_paths", "_refuse_plugin_sources")
            if name in args or name == callee
        ]
        assert order.index("_apply_ignore_paths") < order.index("_refuse_plugin_sources"), (
            f"the plugin scan runs before the prune ({order}), so an excluded "
            f"file can refuse the whole resolution"
        )
