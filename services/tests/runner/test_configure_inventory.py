"""Materialising a configure's resolved inventory (#1972, #2021).

The round-trip class drives REAL ansible, because the claim this module makes is
about what ansible does with the file it writes -- group nesting, which `--limit`
expands through, and per-host variables. A shape assertion on the YAML would
pass on a document ansible resolves to nothing: JSON is valid YAML, so the
stored document handed straight to `-i` parses happily and yields three groups
named `hosts`, `groups` and `children` with no hosts at all. That is the defect
this module exists to prevent, and only running ansible can see it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from terrapod.runner.phases import configure_inventory
from terrapod.runner.phases.configure_inventory import (
    InventoryUnavailable,
    document_to_source,
    materialise,
)
from terrapod.services.ansible_auth import placeholder_for

#: Every shape that carries a distinct risk: a host with variables, a var-less
#: host, a host in no group, direct membership, a declared empty group, and a
#: parent whose ONLY members are inherited through its children.
DOCUMENT = {
    "hosts": {
        "web1": {"ansible_host": "10.0.0.11", "role": "web"},
        "web2": {},
        "db1": {"ansible_host": "10.0.0.21"},
        "loner": {"x": 1},
    },
    "groups": {
        "webservers": ["web1", "web2"],
        "databases": ["db1"],
        "production": [],
        "empty": [],
    },
    "children": {"production": ["webservers", "databases"]},
}


@pytest.fixture(autouse=True)
def _shared_inventory_dir(monkeypatch, tmp_path):
    """Point the shared-volume directory somewhere a test can write.

    The default is `/tp/inventory`, which exists only inside a runner Job -- so
    without this every test that materialises fails `Read-only file system:
    '/tp'`, which reads as a defect in the module rather than as the absence of
    a Job. The one test that asserts WHERE the file goes sets this itself.
    """
    monkeypatch.setenv("TP_EE_INVENTORY_DIR", str(tmp_path / "tp" / "inventory"))


@pytest.fixture
def cfg():
    from terrapod.runner.runner_config import RunnerConfig

    return RunnerConfig.from_env(
        {
            "TP_RUN_ID": "run-1",
            "TP_PHASE": "configure",
            "TP_WORKSPACE_ID": "ws-1",
            "TP_API_URL": "http://api",
            "TP_AUTH_TOKEN": "t",
        }
    )


class TestTheRenderedSource:
    def test_it_is_a_yaml_inventory_rooted_at_all(self):
        """Not the stored document. `-i` needs a source, and the document is not
        one -- its top-level keys would be read as group names."""
        parsed = yaml.safe_load(document_to_source(DOCUMENT))
        assert set(parsed) == {"all"}
        assert set(parsed["all"]) == {"hosts", "children"}

    def test_a_group_known_only_as_a_parent_still_exists(self):
        """A document may name a group in `children` and nowhere else.

        Dropping it would make the nesting dangle, and ansible resolves a
        dangling child as an empty group rather than an error -- so the hosts
        under it disappear with nothing said.
        """
        document = {"hosts": {"h": {}}, "groups": {}, "children": {"p": ["c"]}}
        parsed = yaml.safe_load(document_to_source(document))
        assert set(parsed["all"]["children"]) == {"p", "c"}

    def test_group_variables_are_not_re_emitted(self):
        """They are already inside each host's variables.

        Emitting them at group level too would apply them twice, at a LOWER
        precedence than the host-level copy -- harmless while the two agree and
        a silent divergence the moment anything rewrites one of them.
        """
        source = document_to_source(DOCUMENT)
        for name in DOCUMENT["groups"]:
            entry = yaml.safe_load(source)["all"]["children"][name]
            assert entry is None or "vars" not in entry, (name, entry)

    def test_an_empty_document_is_a_valid_empty_inventory(self):
        """A workspace that resolves to no hosts is a defined answer, not a
        failure -- so the render must not produce something unparseable."""
        parsed = yaml.safe_load(document_to_source({"hosts": {}, "groups": {}, "children": {}}))
        assert parsed == {"all": None}


class TestCredentialSubstitution:
    """#1933. The inventory names a credential; this render substitutes it.

    What ansible DOES with the result is pinned in
    `tests/runner/test_phase_ansible_auth.py`, which drives the real binary.
    These pin the renderer's own contract: that substitution happens here, that
    it reaches every depth, and that an unresolvable reference refuses.
    """

    DOCUMENT = {
        "hosts": {"w1": {"ansible_port": 22}},
        "groups": {"web": ["w1"], "db": []},
        "children": {"infra": ["web"]},
    }

    def _render(self, document=None, resolved=None):
        return json.loads(
            configure_inventory.document_to_source(
                document if document is not None else self.DOCUMENT, resolved=resolved
            )
        )

    def test_no_references_renders_exactly_what_it_rendered_before(self):
        """The parameter is optional and a document with no placeholder must be
        byte-identical either way, or this feature changes what a configure with
        no credentials targets."""
        before = configure_inventory.document_to_source(self.DOCUMENT)
        assert configure_inventory.document_to_source(self.DOCUMENT, resolved={}) == before
        assert configure_inventory.document_to_source(self.DOCUMENT, resolved=None) == before

    def test_a_host_variable_reference_is_substituted(self):
        document = {
            "hosts": {
                "w1": {"ansible_ssh_private_key_file": placeholder_for("web", "private_key")}
            },
            "groups": {},
            "children": {},
        }
        out = self._render(document, {"web.private_key": "/tp/credentials/k"})
        assert out["all"]["hosts"]["w1"]["ansible_ssh_private_key_file"] == "/tp/credentials/k"

    def test_a_reference_inside_a_longer_string_is_substituted(self):
        """The shape a per-group key actually takes in a real estate."""
        document = {
            "hosts": {
                "w1": {
                    "ansible_ssh_common_args": f"-o IdentityFile={placeholder_for('web', 'private_key')}"
                }
            },
            "groups": {},
            "children": {},
        }
        out = self._render(document, {"web.private_key": "/tp/credentials/k"})
        assert out["all"]["hosts"]["w1"]["ansible_ssh_common_args"] == (
            "-o IdentityFile=/tp/credentials/k"
        )

    def test_an_unresolvable_reference_REFUSES_naming_it(self):
        """Neither left in place nor blanked. Left, ansible hands ssh the
        literal as a key path; blanked, it connects with no key -- and both
        arrive as an unreachable host, naming neither the credential nor the
        typo."""
        document = {
            "hosts": {
                "w1": {"ansible_ssh_private_key_file": placeholder_for("typo", "private_key")}
            },
            "groups": {},
            "children": {},
        }
        with pytest.raises(configure_inventory.InventoryUnavailable) as exc:
            self._render(document, {"web.private_key": "/tp/credentials/k"})
        assert "${terrapod:typo.private_key}" in str(exc.value)

    def test_every_unresolvable_reference_is_named_not_just_the_first(self):
        """Fixing one and re-running to find the next is the experience this
        avoids."""
        document = {
            "hosts": {
                "w1": {"a": placeholder_for("one", "private_key")},
                "w2": {"b": placeholder_for("two", "password")},
            },
            "groups": {},
            "children": {},
        }
        with pytest.raises(configure_inventory.InventoryUnavailable) as exc:
            self._render(document, {})
        assert "one.private_key" in str(exc.value) and "two.password" in str(exc.value)

    def test_a_field_typo_is_refused_rather_than_ignored(self):
        """The grammar does not know the field set, so a wrong FIELD is an
        unresolved reference rather than ordinary text that passes through."""
        document = {
            "hosts": {"w1": {"k": "${terrapod:web.privatekey}"}},
            "groups": {},
            "children": {},
        }
        with pytest.raises(configure_inventory.InventoryUnavailable):
            self._render(document, {"web.private_key": "/tp/credentials/k"})

    def test_a_string_that_merely_mentions_terrapod_is_left_alone(self):
        """The negative path: the pattern must not be so loose that ordinary
        prose in a variable becomes a failed reference."""
        document = {
            "hosts": {"w1": {"note": "managed by terrapod, see ${HOME}/notes"}},
            "groups": {},
            "children": {},
        }
        out = self._render(document, {})
        assert out["all"]["hosts"]["w1"]["note"] == "managed by terrapod, see ${HOME}/notes"

    def test_substituting_does_not_disturb_the_hosts_or_the_nesting(self):
        """The nesting is what `--limit` expands through, so a renderer that
        rearranged it would change what a limit selects."""
        plain = json.loads(configure_inventory.document_to_source(self.DOCUMENT))
        out = self._render(self.DOCUMENT, {"web.private_key": "/x"})
        assert out == plain


class TestItFailsClosed:
    """Every refusal here beats its alternative, which is a configure that
    succeeds against a host set nobody chose."""

    def test_a_failed_download_raises(self, cfg, monkeypatch):
        monkeypatch.setattr(
            configure_inventory,
            "download_to_file",
            lambda *a, **k: type("R", (), {"ok": False, "status": 503})(),
        )
        with pytest.raises(InventoryUnavailable) as e:
            materialise(cfg)
        assert "run-1" in str(e.value)

    def test_unreadable_json_raises(self, cfg, monkeypatch):
        def fake(url, dest, **kwargs):
            Path(dest).write_text("<html>nope</html>")
            return type("R", (), {"ok": True, "status": 200})()

        monkeypatch.setattr(configure_inventory, "download_to_file", fake)
        with pytest.raises(InventoryUnavailable) as e:
            materialise(cfg)
        assert "readable JSON" in str(e.value)

    def test_a_json_document_of_the_wrong_shape_raises(self, cfg, monkeypatch):
        """Valid JSON that is not an inventory. Caught rather than rendered,
        because rendering it produces an empty inventory and an empty inventory
        is indistinguishable from a workspace with no hosts."""

        def fake(url, dest, **kwargs):
            Path(dest).write_text(json.dumps({"detail": "not found"}))
            return type("R", (), {"ok": True, "status": 200})()

        monkeypatch.setattr(configure_inventory, "download_to_file", fake)
        with pytest.raises(InventoryUnavailable) as e:
            materialise(cfg)
        assert "not an inventory document" in str(e.value)

    def test_the_run_scoped_url_and_token_are_what_it_asks_with(self, cfg, monkeypatch):
        seen: dict = {}

        def fake(url, dest, **kwargs):
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            Path(dest).write_text(json.dumps(DOCUMENT))
            return type("R", (), {"ok": True, "status": 200})()

        monkeypatch.setattr(configure_inventory, "download_to_file", fake)
        path = materialise(cfg)
        assert seen["url"].endswith("/runs/run-1/artifacts/resolved-inventory")
        assert seen["headers"] == {"Authorization": "Bearer t"}
        assert path.name == configure_inventory.RENDERED_NAME

    def test_it_is_written_where_the_EXECUTION_ENVIRONMENT_can_read_it(
        self, cfg, monkeypatch, tmp_path
    ):
        """The one property no behavioural test of this module would catch.

        ansible runs in the EE container, and the EE mounts only the shared `tp`
        volume and the workspace -- NOT the orchestrator's `tmp`. So writing this
        file where every other substantial runner tempfile goes puts it somewhere
        the process that must read it cannot see, and the configure fails on a
        missing inventory file having downloaded and rendered it perfectly.

        Asserted through `TP_EE_INVENTORY_DIR`, which is what the Job sets, so
        the test fails if the module stops honouring the Job's volume layout.
        """
        shared = tmp_path / "tp" / "inventory"
        monkeypatch.setenv("TP_EE_INVENTORY_DIR", str(shared))

        def fake(url, dest, **kwargs):
            Path(dest).write_text(json.dumps(DOCUMENT))
            return type("R", (), {"ok": True, "status": 200})()

        monkeypatch.setattr(configure_inventory, "download_to_file", fake)
        path = materialise(cfg)
        assert path.parent == shared, path
        assert path.is_file()

    def test_the_default_matches_the_jobs_own_constant(self):
        """Derived, not restated.

        `job_template` creates the directory and passes the path; the default
        here is only for a runner-tier test with no Job. If the two drift, a
        real Job is unaffected and every local test silently writes somewhere
        else -- so the comparison is against the Job builder's own constant.
        """
        from terrapod.runner.job_template import _EE_INVENTORY_DIR

        assert configure_inventory._DEFAULT_INVENTORY_DIR == _EE_INVENTORY_DIR

    def test_the_rendered_file_is_not_written_into_the_fetched_tree(self, cfg, monkeypatch):
        """It goes in its own scratch directory.

        Written beside the operator's own inventory it would become a SECOND
        source of the same hosts the moment their `-i` names a directory, which
        is the ordinary case -- `inventory = hosts.d` in `ansible.cfg`.
        """

        def fake(url, dest, **kwargs):
            Path(dest).write_text(json.dumps(DOCUMENT))
            return type("R", (), {"ok": True, "status": 200})()

        monkeypatch.setattr(configure_inventory, "download_to_file", fake)
        path = materialise(replace(cfg, config_directory="ansible"))
        assert "ansible" not in path.parts[:-1], path


ANSIBLE = shutil.which("ansible-inventory")


@pytest.mark.skipif(ANSIBLE is None, reason="needs a real ansible-inventory on PATH")
class TestTheRoundTripThroughRealAnsible:
    """The rendered source, read back by ansible, reproduces the document.

    Measured against 2.21.5. Each assertion below is a property ansible applies
    and we do not: the host union across `_meta` and the group lists, the direct
    vs inherited membership distinction, and `--limit` expansion through nesting.
    """

    @staticmethod
    def _list(source: str, *extra: str) -> dict:
        d = tempfile.mkdtemp()
        path = os.path.join(d, "inv.yml")
        Path(path).write_text(source)
        env = dict(
            os.environ,
            ANSIBLE_INVENTORY_ENABLED="yaml,ini",
            ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED="True",
        )
        assert ANSIBLE is not None
        proc = subprocess.run(
            [ANSIBLE, "--list", "-i", path, *extra],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        return json.loads(proc.stdout)

    def test_hosts_and_membership_survive_the_round_trip(self):
        from terrapod.services.inventory_resolve import normalise

        got = normalise(self._list(document_to_source(DOCUMENT)))
        assert got["hosts"] == DOCUMENT["hosts"]
        assert got["groups"] == DOCUMENT["groups"]

        # Children compared as a set of edges: list ORDER is not semantic --
        # measured, ansible resolves same-depth group precedence alphabetically
        # regardless of the order a parent lists its children in.
        def edges(children):
            return {(p, c) for p, kids in children.items() for c in kids}

        assert edges(got["children"]) == edges(DOCUMENT["children"])

    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [
            # The one that matters: `production` has no direct members at all,
            # so this only works if the nesting survived the render.
            ("production", {"web1", "web2", "db1"}),
            ("webservers", {"web1", "web2"}),
            ("empty", set()),
        ],
    )
    def test_limit_expands_through_the_nesting(self, pattern, expected):
        out = self._list(document_to_source(DOCUMENT), "--limit", pattern)
        hosts = set((out.get("_meta") or {}).get("hostvars") or {})
        for key, value in out.items():
            if key != "_meta" and isinstance(value, dict):
                hosts |= set(value.get("hosts") or [])
        assert hosts - {"all", "ungrouped"} == expected

    def test_the_stored_document_itself_resolves_to_nothing(self):
        """Why the render exists, asserted rather than asserted in prose.

        The document is valid YAML, so ansible accepts it and reads `hosts`,
        `groups` and `children` as GROUP names -- no error, no hosts. A version
        of this module that forwarded the artifact would look like it worked.
        """
        out = self._list(json.dumps(DOCUMENT))
        assert not (out.get("_meta") or {}).get("hostvars")

    def test_a_host_variable_arrives_intact(self):
        out = self._list(document_to_source(DOCUMENT))
        assert out["_meta"]["hostvars"]["web1"]["ansible_host"] == "10.0.0.11"
        assert out["_meta"]["hostvars"]["web1"]["role"] == "web"

    def test_the_file_MATERIALISE_WRITES_substitutes_the_references(self, cfg, monkeypatch):
        """Through `materialise`, for the same reason as the test below it.

        Measured: dropping `resolved=` from the one `write_text` -- so the
        parameter is accepted and ignored, which is the shape of every
        flag-nothing-reads in this repo -- leaves every other test in this file
        green, because they drive `document_to_source` directly. This is the
        test that fails, and it asserts through REAL ansible so the claim is
        "the credential reaches the host" rather than "a key is in a dict".
        """
        host = next(iter(DOCUMENT["hosts"]))
        document = {
            **DOCUMENT,
            "hosts": {
                **DOCUMENT["hosts"],
                host: {
                    **(DOCUMENT["hosts"][host] or {}),
                    "ansible_ssh_private_key_file": placeholder_for("web-key", "private_key"),
                },
            },
        }

        def fake(url, dest, **kwargs):
            Path(dest).write_text(json.dumps(document))
            return type("R", (), {"ok": True, "status": 200})()

        monkeypatch.setattr(configure_inventory, "download_to_file", fake)
        written = materialise(cfg, resolved={"web-key.private_key": "/tp/credentials/WEB"})

        # No placeholder survives into the file ansible reads...
        assert "${terrapod:" not in written.read_text()
        # ...and ansible resolves the substituted value for that host.
        hostvars = (self._list(written.read_text()).get("_meta") or {}).get("hostvars") or {}
        assert hostvars[host]["ansible_ssh_private_key_file"] == "/tp/credentials/WEB"

    def test_the_file_MATERIALISE_WRITES_resolves_to_the_right_hosts(self, cfg, monkeypatch):
        """Through `materialise`, not through the renderer.

        Every other test in this class calls `document_to_source` directly, so
        they prove the renderer and say NOTHING about whether the module uses
        it. Measured: replacing the one `write_text` with `json.dumps(document)`
        -- i.e. forwarding the artifact, the exact defect this module exists to
        prevent -- left all fifteen of them green. This is the test that fails.
        """

        def fake(url, dest, **kwargs):
            Path(dest).write_text(json.dumps(DOCUMENT))
            return type("R", (), {"ok": True, "status": 200})()

        monkeypatch.setattr(configure_inventory, "download_to_file", fake)
        written = materialise(cfg)

        out = self._list(written.read_text())
        hosts = set((out.get("_meta") or {}).get("hostvars") or {})
        for key, value in out.items():
            if key != "_meta" and isinstance(value, dict):
                hosts |= set(value.get("hosts") or [])
        assert hosts - {"all", "ungrouped"} == set(DOCUMENT["hosts"])
        # And the nesting, because that is what `--limit` walks.
        limited = self._list(written.read_text(), "--limit", "production")
        inherited = set((limited.get("_meta") or {}).get("hostvars") or {})
        for key, value in limited.items():
            if key != "_meta" and isinstance(value, dict):
                inherited |= set(value.get("hosts") or [])
        assert inherited - {"all", "ungrouped"} == {"web1", "web2", "db1"}
