"""A narrowed credential field set must not disarm the credentials already written.

#2425 §0.3. `MACHINE_VALUE_FIELDS` went from six fields to two, `validate_envelope`
refuses an unknown field, and `delivery_entries` used to catch that refusal and
`continue` -- so every credential already carrying `user`, `become`,
`become_method` or `become_user` silently stopped being delivered. The runner then
reported "no credentials were declared, which is an ordinary configure", and the
operator's only signal was a wholly unreachable fleet naming neither the
credential nor the field.

`validate_envelope`'s own comment warns against exactly that -- *"a credential
field under a name nothing reads is a credential that silently does not apply,
and the operator's next signal is a configure that cannot connect"* -- which is
how a guard came to cause the thing it was written to prevent.

The property is an ASYMMETRY, so both halves are instantiated here: tolerated on
read, refused on write. A test of either half alone passes whichever way the code
is written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from terrapod.services import ansible_auth as aa

MACHINE = aa.MACHINE_CATEGORY


def _var(value: str, key: str = "web-key", category: str = MACHINE) -> SimpleNamespace:
    return SimpleNamespace(category=category, key=key, value=value)


LEGACY = json.dumps({"private_key": "KEY", "user": "deploy", "become": "true"})


class TestToleratedOnRead:
    def test_a_legacy_envelope_still_delivers_its_secrets(self):
        # The regression in one assertion: this used to deliver NOTHING.
        entries = aa.delivery_entries([_var(LEGACY)])
        assert len(entries) == 1
        assert json.loads(entries[0]["value"]) == {"private_key": "KEY"}

    def test_the_retired_fields_are_stripped_from_what_is_delivered(self):
        # Not merely ignored downstream: nothing past this point -- the per-run
        # Secret, the credential files, the redactor registry -- should ever see
        # a field it has no home for.
        delivered = json.loads(aa.delivery_entries([_var(LEGACY)])[0]["value"])
        assert not (set(delivered) & aa.RETIRED_MACHINE_FIELDS)

    def test_an_ordinary_credential_is_delivered_BYTE_FOR_BYTE(self):
        # Re-serialising every envelope would be a needless change to what the
        # runner writes, so the stripped form is used only when something was
        # actually stripped.
        #
        # NON-CANONICAL spacing on purpose. Built with `json.dumps` this
        # assertion cannot fail: the fixture and the code under test would share
        # a serialiser, so "always re-serialise" produces an identical string
        # and the test stays green through the mutation it exists to catch --
        # which is exactly what it did before this comment was written.
        raw = '{ "private_key" : "KEY" ,  "password" : "pw" }'
        assert json.loads(raw) == {"private_key": "KEY", "password": "pw"}
        assert raw != json.dumps(json.loads(raw)), "fixture is canonical; the assertion is vacuous"
        assert aa.delivery_entries([_var(raw)])[0]["value"] == raw

    def test_a_vault_credential_is_untouched_by_the_tolerance(self):
        # The retired fields were never vault fields, and tolerating them there
        # would widen a set this change did not touch.
        raw = json.dumps({"password": "pw"})
        assert aa.delivery_entries([_var(raw, category=aa.VAULT_CATEGORY)])[0]["value"] == raw
        with pytest.raises(aa.AnsibleAuthError):
            aa.validate_envelope(
                aa.VAULT_CATEGORY,
                "v",
                json.dumps({"password": "p", "user": "u"}),
                tolerate_retired=True,
            )


class TestRefusedOnWrite:
    @pytest.mark.parametrize("field", sorted(aa.RETIRED_MACHINE_FIELDS))
    def test_each_retired_field_is_still_a_422_on_write(self, field):
        with pytest.raises(aa.AnsibleAuthError) as exc:
            aa.validate_envelope(MACHINE, "web-key", json.dumps({"private_key": "K", field: "x"}))
        # Names the field, because "invalid credential" sends an operator to the
        # wrong place.
        assert field in str(exc.value)

    def test_the_write_path_does_not_pass_the_tolerance(self):
        # The asymmetry is only real while the default is False -- a caller that
        # forgot the keyword would silently accept a retired field.
        import inspect

        sig = inspect.signature(aa.validate_envelope)
        assert sig.parameters["tolerate_retired"].default is False


class TestAnUnusableCredentialIsNoLongerDROPPED:
    def test_a_malformed_envelope_is_delivered_so_the_runner_can_name_it(self):
        # The runner's `apply_credentials` collects it into `failures` and raises
        # `AnsibleAuthUnavailable` naming the category, the key and the reason --
        # which is what the module docstring already claimed and what dropping it
        # here made untrue.
        entries = aa.delivery_entries([_var("not json at all")])
        assert len(entries) == 1
        assert entries[0]["value"] == "not json at all"

    def test_it_is_delivered_UNCHANGED_so_the_report_names_the_real_problem(self):
        bad = json.dumps({"private_key": 7})
        assert aa.delivery_entries([_var(bad)])[0]["value"] == bad

    def test_a_credential_with_neither_secret_is_delivered_rather_than_vanishing(self):
        # Declared and unusable, which the design says is fatal. Dropping it made
        # it indistinguishable from absence, which the design says is fine.
        bad = json.dumps({"known_hosts": "host ssh-ed25519 AAAA"})
        assert len(aa.delivery_entries([_var(bad)])) == 1

    def test_a_non_ansible_variable_is_still_skipped(self):
        # Absence is not an error, and a terraform variable is not a credential.
        assert aa.delivery_entries([_var("x", category="terraform")]) == []
