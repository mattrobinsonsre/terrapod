"""The optional per-phase ServiceAccounts render nothing until an operator asks.

TWO of them now: the apply phase's (#2189) and the configure phase's (#2190).
Every assertion below is parametrized over both rather than copied, because the
mechanism is identical and two copies of it is how one of them comes to render
unconditionally -- the same reason `CREDENTIAL_FILE_MODE` is one producer.

A second identity for the apply phase exists so a speculative plan -- which runs
code from a pull request nobody has reviewed -- cannot reach the credentials that
change infrastructure. A third for the configure phase exists so a playbook
reaching the fleet need not hold the cloud role an apply does. Both are only
worth having if they are also invisible to every deployment that does not want
them: an operator who never sets either must get the same single-account
behaviour they had before, with no new ServiceAccount in their namespace and no
new key in their rendered runner config.

So there are four legs, and three of them are conditional:

  values.yaml                  the default, which must be off
  values.schema.json           the declaration, because the schema refuses
                               undeclared keys
  _helpers.tpl                 the name, which must be EMPTY when unset
  configmap-runner.yaml        the config key, wrapped so it does not render
  serviceaccount-runner.yaml   the account itself, gated on `create`

The empty helper is what makes the rest work: `configmap-runner.yaml` wraps the
key in `with`, and `with` skips a falsy value, so a helper that returned a
default name instead of "" would render the key for everyone and quietly point
every apply at an account that does not exist.

Source-level rather than rendered, matching the rest of this tier: these are Go
templates and the unit tier has no helm binary. The rendered form is asserted in
`ci.yml`'s helm-smoke job, which greps `helm template` output both ways.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_TEMPLATES = _HELM_ROOT / "templates"

#: `(values key, helper name, rendered config key)` for each optional account.
#: Parametrized rather than duplicated, and asserted to be complete below so a
#: third account cannot be added without joining the gate.
_OPTIONAL_ACCOUNTS = [
    ("applyServiceAccount", "terrapod.runnerApplyServiceAccountName", "apply_service_account_name"),
    (
        "configureServiceAccount",
        "terrapod.runnerConfigureServiceAccountName",
        "configure_service_account_name",
    ),
]
_IDS = [a[0] for a in _OPTIONAL_ACCOUNTS]


def _values() -> dict:
    return yaml.safe_load((_HELM_ROOT / "values.yaml").read_text())


def test_every_optional_account_in_values_is_covered_by_this_gate():
    """Derived, so the parametrize list cannot silently fall behind.

    A `*ServiceAccount` key under `runners` that is not `serviceAccount` is an
    optional per-phase account by construction, and every assertion in this file
    keys on the list above -- so an account added to the chart without an entry
    here would be ungated while the file still looked complete.
    """
    declared = {
        k for k in _values()["runners"] if k.endswith("ServiceAccount") and k != "serviceAccount"
    }
    assert declared == set(_IDS), (
        f"the chart declares {sorted(declared)} but this gate covers {sorted(_IDS)}; "
        "add the new account to _OPTIONAL_ACCOUNTS rather than leaving it unasserted"
    )


@pytest.mark.parametrize("values_key,_helper,_config_key", _OPTIONAL_ACCOUNTS, ids=_IDS)
def test_the_default_is_off_so_nothing_changes_for_anyone_who_ignores_it(
    values_key, _helper, _config_key
):
    sa = _values()["runners"][values_key]
    assert sa["create"] is False, "creating the account by default would add one to every namespace"
    assert sa["name"] == "", (
        "a non-empty default name renders the config key for every deployment and "
        "points every apply at an account nobody created"
    )


@pytest.mark.parametrize("values_key,_helper,_config_key", _OPTIONAL_ACCOUNTS, ids=_IDS)
def test_the_schema_declares_it_or_helm_lint_refuses_the_values_file(
    values_key, _helper, _config_key
):
    # values.schema.json sets additionalProperties: false, so an undeclared key
    # in values.yaml is a lint failure rather than a silently ignored setting.
    schema = json.loads((_HELM_ROOT / "values.schema.json").read_text())
    runners = schema["$defs"]["runners"] if "runners" in schema.get("$defs", {}) else None
    if runners is None:  # the chart may declare runners inline
        runners = schema["properties"]["runners"]
    assert values_key in runners["properties"], (
        f"values.yaml sets runners.{values_key}; without a schema entry "
        "`helm lint` fails on the chart's own default values"
    )


@pytest.mark.parametrize("_values_key,helper,_config_key", _OPTIONAL_ACCOUNTS, ids=_IDS)
def test_the_helper_resolves_to_the_empty_string_when_it_is_not_asked_for(
    _values_key, helper, _config_key
):
    body = (_TEMPLATES / "_helpers.tpl").read_text()
    m = re.search(
        r'\{\{-\s*define\s+"' + re.escape(helper) + r'"\s*-?\}\}(.*?)\{\{-\s*end\s*\}\}\s*\n\s*\n',
        body,
        re.DOTALL,
    )
    assert m, f"{helper} has moved or been renamed; update this test rather than deleting it"
    definition = m.group(1)
    # The off branch must default to "" -- anything else is a name for an account
    # that was never created.
    assert re.search(r'default\s+""\s+\$sa\.name', definition), (
        f"{helper} must fall back to the empty string when `create` is false, "
        "because configmap-runner.yaml relies on `with` skipping a falsy value:\n" + definition
    )


@pytest.mark.parametrize("_values_key,helper,config_key", _OPTIONAL_ACCOUNTS, ids=_IDS)
def test_the_config_key_is_wrapped_so_it_does_not_render_when_unset(
    _values_key, helper, config_key
):
    body = (_TEMPLATES / "configmap-runner.yaml").read_text()
    lines = body.split("\n")
    idx = [i for i, line in enumerate(lines) if f"{config_key}:" in line]
    assert len(idx) == 1, f"expected exactly one {config_key} line, found {len(idx)}"
    i = idx[0]
    # The line immediately above must open the conditional. Positional, because a
    # guard written anywhere else in the file does not guard this line -- the same
    # shape as the db.flush()-inside-try gate.
    assert re.search(r'\{\{-\s*with\s+\(include\s+"' + re.escape(helper) + r'"', lines[i - 1]), (
        f"the {config_key} line must sit directly inside a `with` on "
        f"{helper}, or every deployment renders the key:\n"
        f"  {lines[i - 1]!r}\n  {lines[i]!r}"
    )
    # ...and the unconditional sibling above it proves the two are different.
    assert any(line.strip().startswith("service_account_name:") for line in lines), (
        "the plan/default account's key should still render unconditionally"
    )


@pytest.mark.parametrize("values_key,_helper,_config_key", _OPTIONAL_ACCOUNTS, ids=_IDS)
def test_the_account_is_gated_on_create_and_on_there_being_a_listener(
    values_key, _helper, _config_key
):
    body = (_TEMPLATES / "serviceaccount-runner.yaml").read_text()
    gates = [line for line in body.split("\n") if line.startswith("{{- if")]
    named = [g for g in gates if values_key in g]
    assert len(named) == 1, f"expected one gate naming {values_key}, found {len(named)}: {gates}"
    gate = named[0]
    assert ".create" in gate, "the account must render only when `create` is true"
    assert "listener.enabled" in gate, (
        "a runner ServiceAccount is consumed by the listener that creates Jobs, so "
        "a listener-less release must not render one -- the same condition the "
        "default runner account already carries"
    )
