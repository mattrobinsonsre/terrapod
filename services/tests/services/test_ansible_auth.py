"""The Ansible credential envelope, which the API and the runner both read (#1933).

Validation lives at the WRITE boundary, so these are the tests that decide what
an operator can store at all. The envelope's field names are shared with the
runner rather than restated there, so a test here is a test of both sides.
"""

from __future__ import annotations

import json

import pytest

from terrapod.services import ansible_auth as aa
from terrapod.services.inventory_resolution import (
    InventoryValidationError,
    validate_machine_scope,
)


class TestItIsPure:
    """It ships in the runner image, where an import is the whole hazard."""

    def test_it_imports_nothing_from_terrapod_and_nothing_third_party(self):
        """A module-level import of anything heavier breaks EVERY run.

        `job_entrypoint` imports the configure phase at module level and the
        phase imports this, so a missing dependency here is not a configure
        that fails -- it is a plan that cannot start. That is not hypothetical:
        it reached a real runner pod when `inventory_render` imported yaml
        against a 21-package requirements file.
        """
        import ast
        import pathlib
        import sys

        src = pathlib.Path(aa.__file__)
        tree = ast.parse(src.read_text())
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.col_offset == 0:
                mods.add(node.module.split(".")[0])
        assert "terrapod" not in mods, f"{src.name} must not import Terrapod: {sorted(mods)}"
        extra = mods - set(sys.stdlib_module_names)
        assert not extra, f"{src.name} must be stdlib-only; it imports {sorted(extra)}"


class TestTheVaultEnvelope:
    def test_a_password_is_required(self):
        with pytest.raises(aa.AnsibleAuthError, match="password"):
            aa.validate_envelope(aa.VAULT_CATEGORY, "prod", json.dumps({}))

    def test_an_empty_password_is_refused_rather_than_stored(self):
        """Stored, it would produce a configure that fails mid-play instead."""
        with pytest.raises(aa.AnsibleAuthError, match="password"):
            aa.validate_envelope(aa.VAULT_CATEGORY, "prod", json.dumps({"password": ""}))

    def test_a_valid_envelope_comes_back_parsed(self):
        out = aa.validate_envelope(aa.VAULT_CATEGORY, "prod", json.dumps({"password": "p"}))
        assert out == {"password": "p"}

    @pytest.mark.parametrize("label", ["a,b", "a@b", "", "-leading", "has space"])
    def test_a_label_that_would_corrupt_the_identity_list_is_refused(self, label):
        """`,` and `@` are ANSIBLE_VAULT_IDENTITY_LIST's own delimiters.

        A label carrying one does not produce a rejected list -- it produces a
        DIFFERENT list, silently, which is the whole reason this is refused at
        the write rather than escaped at the read.
        """
        with pytest.raises(aa.AnsibleAuthError, match="label"):
            aa.validate_envelope(aa.VAULT_CATEGORY, label, json.dumps({"password": "p"}))

    @pytest.mark.parametrize("label", ["prod", "dev_2", "eu-west-1", "a.b", "_x", "9lives"])
    def test_the_labels_an_operator_would_actually_write_are_accepted(self, label):
        assert aa.validate_envelope(aa.VAULT_CATEGORY, label, json.dumps({"password": "p"}))


class TestTheMachineEnvelope:
    def test_it_needs_a_key_or_a_password(self):
        """With neither there is nothing to authenticate with, so the credential
        does nothing -- and does it silently, as an unreachable fleet."""
        with pytest.raises(aa.AnsibleAuthError, match="private_key"):
            aa.validate_envelope(aa.MACHINE_CATEGORY, "all", json.dumps({"user": "deploy"}))

    def test_either_one_alone_is_enough(self):
        assert aa.validate_envelope(aa.MACHINE_CATEGORY, "all", json.dumps({"private_key": "k"}))
        assert aa.validate_envelope(aa.MACHINE_CATEGORY, "all", json.dumps({"password": "p"}))

    def test_an_unknown_field_is_refused_and_named(self):
        """Ignored, it would be a credential field nothing reads -- a credential
        that silently does not apply."""
        with pytest.raises(aa.AnsibleAuthError, match="become_pass"):
            aa.validate_envelope(
                aa.MACHINE_CATEGORY,
                "all",
                json.dumps({"private_key": "k", "become_pass": "p"}),
            )

    def test_every_documented_field_is_accepted_together(self):
        """Derived from the field maps, so a field added to one and forgotten
        in the other fails here rather than at a configure."""
        envelope = dict.fromkeys(sorted(aa.MACHINE_FIELDS), "x")
        envelope["become"] = "yes"
        assert aa.validate_envelope(aa.MACHINE_CATEGORY, "all", json.dumps(envelope))

    @pytest.mark.parametrize("raw", ["true", "yes", "1", "on", "FALSE", "no", "off"])
    def test_the_booleans_an_operator_would_write_are_accepted(self, raw):
        assert aa.validate_envelope(
            aa.MACHINE_CATEGORY, "all", json.dumps({"password": "p", "become": raw})
        )

    def test_a_typo_in_become_is_refused_rather_than_read_as_false(self):
        """Read as false it would silently drop privilege escalation, and the
        operator's next signal is a playbook that cannot write anything."""
        with pytest.raises(aa.AnsibleAuthError, match="boolean"):
            aa.validate_envelope(
                aa.MACHINE_CATEGORY, "all", json.dumps({"password": "p", "become": "ture"})
            )


class TestTheEnvelopeItself:
    @pytest.mark.parametrize("value", ["", "   ", "not json", "[1,2]", '"a string"', "7"])
    def test_anything_that_is_not_an_object_is_refused(self, value):
        with pytest.raises(aa.AnsibleAuthError):
            aa.validate_envelope(aa.VAULT_CATEGORY, "prod", value)

    def test_a_non_string_field_is_refused(self):
        """It cannot be written to a credential file or an inventory variable."""
        with pytest.raises(aa.AnsibleAuthError, match="string"):
            aa.validate_envelope(aa.VAULT_CATEGORY, "prod", json.dumps({"password": 7}))

    def test_another_category_is_passed_over_rather_than_refused(self):
        """The shared write guard sees every category, so this is called for a
        plain terraform variable too."""
        assert aa.validate_envelope("native", "region", "eu-west-1") == {}
        assert aa.validate_envelope("git_ssh_auth", "github.com", "{}") == {}


class TestTheMachineScope:
    def test_all_is_accepted_because_it_is_ansibles_own_root_group(self):
        assert validate_machine_scope("all") == "all"

    def test_a_group_name_is_accepted(self):
        assert validate_machine_scope("web_servers") == "web_servers"

    def test_ungrouped_is_refused_with_its_reason(self):
        """Its membership changes whenever any group does, so scoping a
        credential to it promises something the operator cannot control."""
        with pytest.raises(InventoryValidationError, match="ungrouped"):
            validate_machine_scope("ungrouped")

    @pytest.mark.parametrize("scope", ["", "9web", "web-servers", "web servers", "a:b"])
    def test_a_name_ansible_would_not_resolve_to_a_group_is_refused(self, scope):
        with pytest.raises(InventoryValidationError):
            validate_machine_scope(scope)

    def test_it_shares_the_group_rule_rather_than_restating_it(self):
        """The two must agree on everything but `all`, or a credential could be
        scoped to a name no group can have."""
        from terrapod.services.inventory_resolution import validate_group_name

        for name in ("web", "_x", "a9", "db_primary"):
            assert validate_machine_scope(name) == validate_group_name(name)


class TestDeliveryEntries:
    class _Row:
        def __init__(self, category, key, value):
            self.category, self.key, self.value = category, key, value

    def _rows(self):
        return [
            self._Row(aa.VAULT_CATEGORY, "prod", json.dumps({"password": "p"})),
            self._Row(aa.MACHINE_CATEGORY, "web", json.dumps({"private_key": "k"})),
            self._Row("native", "region", "eu-west-1"),
            self._Row("git_ssh_auth", "github.com", json.dumps({"private_key": "k"})),
        ]

    def test_only_the_two_ansible_categories_are_carried(self):
        out = aa.delivery_entries(self._rows())
        assert [(e["category"], e["key"]) for e in out] == [
            (aa.VAULT_CATEGORY, "prod"),
            (aa.MACHINE_CATEGORY, "web"),
        ]

    def test_a_row_written_before_the_write_guard_existed_is_dropped_not_fatal(self):
        """Failing the run for it would make a historical row un-runnable rather
        than merely inert. A credential that is readable and UNUSABLE is the
        different case, and the runner raises for that one."""
        rows = [*self._rows(), self._Row(aa.VAULT_CATEGORY, "legacy", "not json")]
        assert all(e["key"] != "legacy" for e in aa.delivery_entries(rows))

    def test_the_value_is_carried_byte_for_byte(self):
        """Never reserialised: the runner parses it, and a round trip through
        json here would be a second place the envelope's shape is decided."""
        raw = json.dumps({"password": "p"}, indent=4)
        out = aa.delivery_entries([self._Row(aa.VAULT_CATEGORY, "prod", raw)])
        assert out[0]["value"] == raw
