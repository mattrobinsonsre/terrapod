"""The Ansible credential envelope, which the API and the runner both read (#1933).

Validation lives at the WRITE boundary, so these are the tests that decide what
an operator can store at all. The envelope's field names are shared with the
runner rather than restated there, so a test here is a test of both sides.
"""

from __future__ import annotations

import json

import pytest

from terrapod.services import ansible_auth as aa


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
        assert aa.validate_envelope(aa.MACHINE_CATEGORY, "all", json.dumps(envelope))

    @pytest.mark.parametrize("field", ["user", "become", "become_method", "become_user"])
    def test_a_non_secret_connection_setting_is_refused(self, field):
        """#2413 F9. These four used to be accepted and substituted verbatim,
        which put a plain value into the resolved inventory -- the opposite of
        what this category exists to do. They are inventory variables: write
        `ansible_user: deploy` in the inventory, where it is visible, can differ
        per host and needs no indirection through a shared credential.

        Refused rather than ignored, so an operator who writes one is told. The
        envelope has always refused an unknown field, so this is that rule
        applying to four fewer known ones."""
        with pytest.raises(aa.AnsibleAuthError):
            aa.validate_envelope(
                aa.MACHINE_CATEGORY, "all", json.dumps({"password": "p", field: "x"})
            )

    def test_the_two_secrets_that_remain_are_still_accepted(self):
        """The other half of the narrowing: `become_password` stays because it
        IS a secret and ansible has no `ansible_become_password_file`, so it
        can only ever be a variable -- which is why it needs the redactor."""
        assert aa.validate_envelope(
            aa.MACHINE_CATEGORY,
            "all",
            json.dumps({"password": "p", "become_password": "s"}),
        )

    def test_the_value_fields_are_exactly_the_secrets(self):
        """Derived rather than restated: if a non-secret is ever added back,
        this fails instead of the review catching it again."""
        assert aa.MACHINE_VALUE_FIELDS <= aa.MACHINE_SECRET_FIELDS, (
            "every substituted field must be a secret -- a non-secret belongs "
            "in the inventory (#2413 F9)"
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


class TestTheCredentialName:
    """`key` is the credential's NAME, not a scope (#1933).

    Placement is said in the inventory by a placeholder, so there is nothing
    here about hosts or groups -- the name only has to be a string a
    placeholder can carry, and the same rule serves both categories because a
    vault label is also a name.
    """

    @pytest.mark.parametrize("category", [aa.MACHINE_CATEGORY, aa.VAULT_CATEGORY])
    @pytest.mark.parametrize("name", ["web", "web-key", "db.primary", "_x", "9lives", "a_b-c.d"])
    def test_a_usable_name_is_accepted(self, category, name):
        envelope = {"password": "p"} if category == aa.VAULT_CATEGORY else {"private_key": "k"}
        assert aa.validate_envelope(category, name, json.dumps(envelope))

    @pytest.mark.parametrize("category", [aa.MACHINE_CATEGORY, aa.VAULT_CATEGORY])
    @pytest.mark.parametrize("name", ["", "a,b", "a@b", "has space", "-leading", "a:b", "a{b}"])
    def test_a_name_a_placeholder_could_not_carry_is_refused(self, category, name):
        """The character set is the PLACEHOLDER's, not an inventory name's: a
        reference that does not match the pattern is not a rejected reference,
        it is a literal string handed to ssh as a key path."""
        envelope = {"password": "p"} if category == aa.VAULT_CATEGORY else {"private_key": "k"}
        with pytest.raises(aa.AnsibleAuthError, match="name"):
            aa.validate_envelope(category, name, json.dumps(envelope))

    def test_the_refusal_says_why_per_category(self):
        """A vault label and a machine name are refused for different reasons --
        an identity list's delimiters, and a placeholder's grammar -- and an
        operator fixing one needs to know which."""
        with pytest.raises(aa.AnsibleAuthError, match="IDENTITY_LIST"):
            aa.validate_envelope(aa.VAULT_CATEGORY, "a,b", json.dumps({"password": "p"}))
        with pytest.raises(aa.AnsibleAuthError, match=r"terrapod:"):
            aa.validate_envelope(aa.MACHINE_CATEGORY, "a,b", json.dumps({"private_key": "k"}))

    def test_every_name_the_validator_accepts_round_trips_through_a_placeholder(self):
        """The two rules must agree, or a name the API stored could not be
        referred to. Derived rather than listed: whatever `CREDENTIAL_NAME`
        accepts, `PLACEHOLDER` must match back."""
        for name in ("web", "web-key", "db.primary", "_x", "9lives"):
            for field in sorted(aa.MACHINE_FIELDS):
                text = aa.placeholder_for(name, field)
                match = aa.PLACEHOLDER.fullmatch(text)
                assert match, f"{text} is not matched by PLACEHOLDER"
                assert match.group(1) == name and match.group(2) == field


class TestThePlaceholder:
    """The grammar, and the reason it is a string rather than a `!terrapod` tag."""

    def test_it_is_an_ordinary_string_a_generic_yaml_parser_accepts(self):
        """Measured, and the reason the tag form was rejected: `ansible-inventory`
        cannot parse an unknown YAML tag and fails SILENTLY, exiting 0 with zero
        hosts resolved. A `${...}` string is a plain scalar to every parser."""
        text = aa.placeholder_for("web", "private_key")
        assert text == "${terrapod:web.private_key}"
        assert not text.startswith("!")

    def test_one_producer_for_the_grammar(self):
        """`placeholder_for` and `PLACEHOLDER` must agree, or the docs show a
        form the code does not resolve."""
        assert aa.PLACEHOLDER.fullmatch(aa.placeholder_for("n", "private_key"))

    def test_substitution_reaches_inside_a_longer_string(self):
        """The case that makes this better than scoping: a per-group key is
        commonly expressed through `ansible_ssh_common_args`, where the
        reference is one fragment of an ssh argument list."""
        value = f"-o IdentityFile={aa.placeholder_for('web', 'private_key')} -o Port=22"
        out = aa.substitute(value, {"web.private_key": "/tp/credentials/k"})
        assert out == "-o IdentityFile=/tp/credentials/k -o Port=22"

    def test_it_substitutes_at_every_depth(self):
        doc = {
            "all": {
                "hosts": {"h1": {"k": aa.placeholder_for("web", "private_key")}},
                "vars": {"list": [aa.placeholder_for("web", "user")]},
            }
        }
        out = aa.substitute(doc, {"web.private_key": "/p", "web.user": "deploy"})
        assert out["all"]["hosts"]["h1"]["k"] == "/p"
        assert out["all"]["vars"]["list"] == ["deploy"]

    def test_a_non_string_passes_through_untouched(self):
        assert aa.substitute({"a": 1, "b": True, "c": None}, {}) == {"a": 1, "b": True, "c": None}

    def test_an_unknown_reference_is_LEFT_rather_than_blanked(self):
        """So the caller can refuse naming it. Blanked, ansible would connect
        with no key and the failure would arrive as an unreachable host, naming
        neither the credential nor the reference."""
        text = aa.placeholder_for("missing", "private_key")
        assert aa.substitute(text, {}) == text

    def test_unresolved_reports_every_reference_deduplicated(self):
        doc = {
            "a": aa.placeholder_for("x", "private_key"),
            "b": aa.placeholder_for("x", "private_key"),
            "c": [aa.placeholder_for("y", "password")],
        }
        assert aa.unresolved(doc) == [
            aa.placeholder_for("x", "private_key"),
            aa.placeholder_for("y", "password"),
        ]

    def test_unresolved_is_empty_once_substituted(self):
        doc = {"a": aa.placeholder_for("x", "private_key")}
        assert aa.unresolved(aa.substitute(doc, {"x.private_key": "/p"})) == []

    def test_a_field_name_that_is_not_a_field_still_parses_as_a_reference(self):
        """Deliberately: the grammar does not know the field set, so a typo in
        the FIELD is reported as an unresolved reference by the renderer rather
        than silently ignored as ordinary text."""
        assert aa.PLACEHOLDER.fullmatch("${terrapod:web.not_a_field}")


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

    def test_an_unreadable_row_is_DELIVERED_so_the_runner_names_it(self):
        """It used to be dropped here, and #2425 §0.3 is why it is not.

        The old reason was that a bad envelope at this point meant a row written
        before the write guard existed, so failing the run for it would make a
        historical row un-runnable rather than merely inert. What falsified that
        is the narrowing: the commonest bad envelope became a good credential
        carrying one retired field, dropping it disarmed the fleet, and "merely
        inert" reached the operator as a configure whose every host was
        unreachable naming nothing.

        So the entry goes through UNCHANGED and the runner refuses:
        `phases/ansible_auth.run` collects it into `failures` and raises
        `AnsibleAuthUnavailable` naming the category, the key and the reason.
        Unchanged rather than stripped, so what it reports is the real problem.

        The retired-field class -- the one that made this worth changing -- is
        delivered and WORKS; `test_retired_credential_fields.py` is that half.
        """
        rows = [*self._rows(), self._Row(aa.VAULT_CATEGORY, "legacy", "not json")]
        out = aa.delivery_entries(rows)
        legacy = [e for e in out if e["key"] == "legacy"]
        assert legacy, "an unreadable credential must not be dropped silently"
        # Byte-for-byte, so the runner's error quotes what is actually stored.
        assert legacy[0]["value"] == "not json"
        # And it has not cost the good credentials beside it their delivery.
        assert [e["key"] for e in out] == ["prod", "web", "legacy"]

    def test_nothing_in_this_module_can_log_because_it_ships_in_the_runner(self):
        """The no-drop decision above deliberately emits no warning.

        A log line was the obvious way to make the drop non-silent, and it is
        not available: this module is stdlib-only (`TestItIsPure`) because a
        module-level import here breaks every run, not merely every configure.
        Nor is it wanted -- the run error reaches the operator in the log they
        are already reading.

        It needs its OWN test rather than leaning on `TestItIsPure`, because the
        tempting form is a stdlib one: `logging.getLogger(__name__)` imports
        nothing third-party, so the purity gate passes on it. Measured -- that
        mutation fails here and nowhere else.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(aa.__file__).read_text())
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id in {"logger", "log", "logging"}
        ]
        assert not calls, f"ansible_auth.py logs at {[n.lineno for n in calls]}"

    def test_the_value_is_carried_byte_for_byte(self):
        """Never reserialised: the runner parses it, and a round trip through
        json here would be a second place the envelope's shape is decided."""
        raw = json.dumps({"password": "p"}, indent=4)
        out = aa.delivery_entries([self._Row(aa.VAULT_CATEGORY, "prod", raw)])
        assert out[0]["value"] == raw
