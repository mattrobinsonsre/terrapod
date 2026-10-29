"""The two dynamic-inventory switches, runner side (#2033).

The API refuses the `script` and `auto` inventory plugins permanently, so a
workspace that opts in has them resolved here instead: this Job has no database
URL, no KEK and an identity scoped to one run, and it already executes the
operator's playbook. Two mechanisms, and both are only meaningful as a
difference, so both directions are driven:

* `configure._enabled_plugins` composes `ANSIBLE_INVENTORY_ENABLED` per run,
  keeping ansible's own relative order -- the order is the order plugins are
  offered a file, so it decides which claims an ambiguous name;
* `configure_inventory.materialise` returns the `-i` LIST, which is the merged
  artifact alone in the ordinary case and the repository's own sources plus the
  declared rows when the artifact carries a handoff.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from terrapod.runner.phases import configure, configure_inventory
from terrapod.runner.phases.configure_inventory import InventoryUnavailable, materialise
from terrapod.runner.runner_config import RunnerConfig


@pytest.fixture
def cfg():
    base = RunnerConfig.from_env(
        {
            "TP_RUN_ID": "run-1",
            "TP_PHASE": "configure",
            "TP_WORKSPACE_ID": "ws-1",
            "TP_API_URL": "http://api",
            "TP_AUTH_TOKEN": "t",
        }
    )
    return replace(base, config_directory="ansible", playbook="site.yml", ansible_limit="")


class TestWhichPluginsAreEnabled:
    """`yaml,ini` is the floor and is what every run got before the switches."""

    def test_neither_switch_is_the_floor(self, cfg):
        assert configure._enabled_plugins(cfg) == "yaml,ini"

    def test_scripts_only(self, cfg):
        assert (
            configure._enabled_plugins(replace(cfg, inventory_scripts_enabled=True))
            == "script,yaml,ini"
        )

    def test_dynamic_only(self, cfg):
        assert (
            configure._enabled_plugins(replace(cfg, dynamic_inventory_enabled=True))
            == "auto,yaml,ini"
        )

    def test_both_keep_ansibles_own_relative_order(self, cfg):
        """`script` before `auto` before `yaml` before `ini`, as in ansible's
        documented default. The order decides which plugin claims an ambiguous
        name, so reordering it would be a behaviour change dressed as a
        tidy-up."""
        assert (
            configure._enabled_plugins(
                replace(cfg, inventory_scripts_enabled=True, dynamic_inventory_enabled=True)
            )
            == "script,auto,yaml,ini"
        )

    def test_each_switch_reaches_the_forced_environment_on_its_own(self, cfg):
        """A golden scenario pins the Job spec; this pins the env ansible gets.

        Separately per switch, because a change that conflated the two would
        pass a test that only ever set both.
        """
        floor = configure._forced_env(cfg)["ANSIBLE_INVENTORY_ENABLED"]
        scripts = configure._forced_env(replace(cfg, inventory_scripts_enabled=True))
        dynamic = configure._forced_env(replace(cfg, dynamic_inventory_enabled=True))
        assert floor == "yaml,ini"
        assert "script" in scripts["ANSIBLE_INVENTORY_ENABLED"]
        assert "auto" not in scripts["ANSIBLE_INVENTORY_ENABLED"]
        assert "auto" in dynamic["ANSIBLE_INVENTORY_ENABLED"]
        assert "script" not in dynamic["ANSIBLE_INVENTORY_ENABLED"]


_MERGED = {"hosts": {"declared-1": {}}, "groups": {}, "children": {}}
_HANDOFF = {
    **_MERGED,
    "partial": True,
    "skipped_sources": [
        {"path": "ansible/hosts.d/30_aws.yml", "kind": "plugin", "plugin": "aws_ec2", "reason": "r"}
    ],
    "sources": ["ansible/hosts.d/10_static.yml", "ansible/hosts.d/30_aws.yml"],
    "declared": {"all": {"hosts": {"declared-1": {}}}},
}


def _served(monkeypatch, document: dict) -> None:
    def fake(url, dest, **kwargs):  # noqa: ARG001
        Path(dest).write_text(json.dumps(document))
        return type("R", (), {"ok": True, "status": 200})()

    monkeypatch.setattr(configure_inventory, "download_to_file", fake)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fetched tree holding the sources the handoff names, and a target dir."""
    sources = tmp_path / "tree" / "ansible" / "hosts.d"
    sources.mkdir(parents=True)
    (sources / "10_static.yml").write_text("all:\n  hosts:\n    web1:\n")
    (sources / "30_aws.yml").write_text("plugin: amazon.aws.aws_ec2\n")
    monkeypatch.setenv("TP_EE_INVENTORY_DIR", str(tmp_path / "inv"))
    return tmp_path / "tree"


class TestTheOrdinaryCaseIsUnchanged:
    def test_a_document_with_no_handoff_is_one_rendered_artifact(self, cfg, monkeypatch, tree):
        _served(monkeypatch, _MERGED)
        out = materialise(cfg, work_dir=tree)
        assert [p.name for p in out] == [configure_inventory.RENDERED_NAME]

    def test_sources_without_declared_rows_is_not_a_handoff(self, cfg, monkeypatch, tree):
        """Both keys or neither. A half-written handoff must not put the runner
        on a path where it re-reads the repository and has nothing to layer the
        declared rows from -- that would silently drop every Terraform-declared
        host."""
        _served(monkeypatch, {**_MERGED, "sources": ["ansible/hosts.d/10_static.yml"]})
        out = materialise(cfg, work_dir=tree)
        assert [p.name for p in out] == [configure_inventory.RENDERED_NAME]


class TestTheHandoff:
    def test_the_repository_sources_come_first_in_the_operators_own_order(
        self, cfg, monkeypatch, tree
    ):
        """Ansible's `-i` order is precedence order, and within an inventory
        directory the operator chooses it by filename -- the real arrangement is
        a `hosts.d` of numerically-prefixed files. So the list is theirs, in
        their order, and nothing here reorders it."""
        _served(monkeypatch, _HANDOFF)
        out = materialise(cfg, work_dir=tree)
        assert [str(p) for p in out[:2]] == [
            str((tree / "ansible/hosts.d/10_static.yml").resolve()),
            str((tree / "ansible/hosts.d/30_aws.yml").resolve()),
        ]

    def test_the_declared_rows_come_last_because_the_platform_source_wins(
        self, cfg, monkeypatch, tree
    ):
        """The same relationship a Terraform variable has to a default written
        into the configuration."""
        _served(monkeypatch, _HANDOFF)
        out = materialise(cfg, work_dir=tree)
        assert out[-1].name == configure_inventory.DECLARED_NAME
        written = json.loads(out[-1].read_text())
        assert written == {"all": {"hosts": {"declared-1": {}}}}

    def test_the_MERGED_document_is_NOT_in_the_list(self, cfg, monkeypatch, tree):
        """Its host variables are EFFECTIVE -- the API collapsed `group_vars`
        into them -- so appending it beside a fresh read of the same repository
        would re-assert every repository group variable at HOST precedence,
        silently outranking a value a dynamic source contributed for a host both
        describe. The repository is read once, here."""
        _served(monkeypatch, _HANDOFF)
        out = materialise(cfg, work_dir=tree)
        assert configure_inventory.RENDERED_NAME not in [p.name for p in out]

    def test_a_named_source_missing_from_the_tree_RAISES(self, cfg, monkeypatch, tree):
        """Refused rather than skipped: a missing source resolves fewer hosts,
        and a configure that reaches part of a fleet and reports success is the
        one outcome worth refusing a runner slot over."""
        (tree / "ansible/hosts.d/30_aws.yml").unlink()
        _served(monkeypatch, _HANDOFF)
        with pytest.raises(InventoryUnavailable) as exc:
            materialise(cfg, work_dir=tree)
        assert "30_aws.yml" in str(exc.value), "the refusal must name the source"

    def test_the_declared_file_is_written_at_the_credential_mode(self, cfg, monkeypatch, tree):
        """A machine credential's connection password reaches ansible as a
        VARIABLE -- there is no `ansible_password_file` -- so a declared row can
        carry a secret and this file is secret-bearing whenever one is
        declared."""
        from terrapod.services.ansible_auth import CREDENTIAL_FILE_MODE

        _served(monkeypatch, _HANDOFF)
        out = materialise(cfg, work_dir=tree)
        assert out[-1].stat().st_mode & 0o777 == CREDENTIAL_FILE_MODE

    def test_a_placeholder_in_a_declared_row_is_substituted(self, cfg, monkeypatch, tree):
        """Substitution applies to the handoff's declared rows exactly as to the
        merged document: a `${terrapod:...}` is a credential reference wherever
        the merge happened."""
        from terrapod.services.ansible_auth import placeholder_for

        document = {
            **_HANDOFF,
            "declared": {
                "all": {
                    "hosts": {
                        "web1": {
                            "ansible_ssh_private_key_file": placeholder_for("k", "private_key")
                        }
                    }
                }
            },
        }
        _served(monkeypatch, document)
        out = materialise(cfg, work_dir=tree, resolved={"k.private_key": "/tp/credentials/K"})
        written = json.loads(out[-1].read_text())
        assert (
            written["all"]["hosts"]["web1"]["ansible_ssh_private_key_file"] == "/tp/credentials/K"
        )

    def test_an_unresolved_placeholder_in_a_declared_row_RAISES(self, cfg, monkeypatch, tree):
        from terrapod.services.ansible_auth import placeholder_for

        document = {
            **_HANDOFF,
            "declared": {"all": {"hosts": {"web1": {"k": placeholder_for("nope", "password")}}}},
        }
        _served(monkeypatch, document)
        with pytest.raises(InventoryUnavailable) as exc:
            materialise(cfg, work_dir=tree)
        assert "nope.password" in str(exc.value)
