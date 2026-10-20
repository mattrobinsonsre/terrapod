"""The apply ServiceAccount renders nothing at all until an operator asks for it.

A second identity for the apply phase exists so a speculative plan -- which runs
code from a pull request nobody has reviewed -- cannot reach the credentials that
change infrastructure. That is only worth having if it is also invisible to every
deployment that does not want it: an operator who never sets it must get the same
single-account behaviour they had before, with no new ServiceAccount in their
namespace and no new key in their rendered runner config.

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

import yaml

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_TEMPLATES = _HELM_ROOT / "templates"
_HELPER = "terrapod.runnerApplyServiceAccountName"


def _values() -> dict:
    return yaml.safe_load((_HELM_ROOT / "values.yaml").read_text())


def test_the_default_is_off_so_nothing_changes_for_anyone_who_ignores_it():
    sa = _values()["runners"]["applyServiceAccount"]
    assert sa["create"] is False, "creating the account by default would add one to every namespace"
    assert sa["name"] == "", (
        "a non-empty default name renders the config key for every deployment and "
        "points every apply at an account nobody created"
    )


def test_the_schema_declares_it_or_helm_lint_refuses_the_values_file():
    # values.schema.json sets additionalProperties: false, so an undeclared key
    # in values.yaml is a lint failure rather than a silently ignored setting.
    schema = json.loads((_HELM_ROOT / "values.schema.json").read_text())
    runners = schema["$defs"]["runners"] if "runners" in schema.get("$defs", {}) else None
    if runners is None:  # the chart may declare runners inline
        runners = schema["properties"]["runners"]
    assert "applyServiceAccount" in runners["properties"], (
        "values.yaml sets runners.applyServiceAccount; without a schema entry "
        "`helm lint` fails on the chart's own default values"
    )


def test_the_helper_resolves_to_the_empty_string_when_it_is_not_asked_for():
    body = (_TEMPLATES / "_helpers.tpl").read_text()
    m = re.search(
        r'\{\{-\s*define\s+"' + re.escape(_HELPER) + r'"\s*-?\}\}(.*?)\{\{-\s*end\s*\}\}\s*\n\s*\n',
        body,
        re.DOTALL,
    )
    assert m, f"{_HELPER} has moved or been renamed; update this test rather than deleting it"
    definition = m.group(1)
    # The off branch must default to "" -- anything else is a name for an account
    # that was never created.
    assert re.search(r'default\s+""\s+\$sa\.name', definition), (
        f"{_HELPER} must fall back to the empty string when `create` is false, "
        "because configmap-runner.yaml relies on `with` skipping a falsy value:\n" + definition
    )


def test_the_config_key_is_wrapped_so_it_does_not_render_when_unset():
    body = (_TEMPLATES / "configmap-runner.yaml").read_text()
    lines = body.split("\n")
    idx = [i for i, line in enumerate(lines) if "apply_service_account_name:" in line]
    assert len(idx) == 1, f"expected exactly one apply_service_account_name line, found {len(idx)}"
    i = idx[0]
    # The line immediately above must open the conditional. Positional, because a
    # guard written anywhere else in the file does not guard this line -- the same
    # shape as the db.flush()-inside-try gate.
    assert re.search(r'\{\{-\s*with\s+\(include\s+"' + re.escape(_HELPER) + r'"', lines[i - 1]), (
        "the apply_service_account_name line must sit directly inside a `with` on "
        f"{_HELPER}, or every deployment renders the key:\n"
        f"  {lines[i - 1]!r}\n  {lines[i]!r}"
    )
    # ...and the unconditional sibling above it proves the two are different.
    assert any("service_account_name:" in line and "apply" not in line for line in lines), (
        "the plan/default account's key should still render unconditionally"
    )


def test_the_account_is_gated_on_create_and_on_there_being_a_listener():
    body = (_TEMPLATES / "serviceaccount-runner.yaml").read_text()
    gates = [line for line in body.split("\n") if line.startswith("{{- if")]
    apply_gates = [g for g in gates if "applyServiceAccount" in g]
    assert len(apply_gates) == 1, (
        f"expected one gate naming applyServiceAccount, found {len(apply_gates)}: {gates}"
    )
    gate = apply_gates[0]
    assert ".create" in gate, "the account must render only when `create` is true"
    assert "listener.enabled" in gate, (
        "a runner ServiceAccount is consumed by the listener that creates Jobs, so "
        "a listener-less release must not render one -- the same condition the "
        "default runner account already carries"
    )
