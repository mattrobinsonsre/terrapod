"""Every `artifact_retention` setting must be reachable through the chart.

Three gates look at this block and none of them asked this question.
`test_values_contract` snapshots the keys that are **in values.yaml**;
`helm-smoke`'s retention check derives its subject from values.yaml too
(`awk '/^    artifact_retention:/...'`) and asserts each one renders; and
`test_config_contract` snapshots the keys `Settings` **accepts**. So a real
`Settings` field that never reached values.yaml is outside all three subjects at
once, and every gate stays green while an operator cannot set it.

That is not hypothetical: `config_versions_keep` shipped that way (#2316). It is a
fully implemented cleanup category — `artifact_retention_service` dispatches
`config_versions_count` on it — and `values.schema.json` is
`additionalProperties: false`, so an operator who followed
`docs/artifact-retention.md`'s own tuning snippet did not get a silently-inert
value, they got a hard rejection:

    at '/api/config/artifact_retention': additional properties
    'config_versions_keep' not allowed

**The lesson this file exists to pin is about derivation, not about one key.** A
derived gate's *derivation* decides its blind spot, and all three gates above
derive from a place that already presupposes the key is reachable. This one
derives from the **model**, which is the only source that cannot omit a field and
still be the model.

Scoped deliberately to `ArtifactRetentionConfig` rather than to every `Settings`
sub-model. The general inverse — "every setting is chart-reachable" — is false by
design and would need a ledger: a secret such as `slack.bot_token` or
`registry.signing_key` is injected via `secretKeyRef` and must never be rendered
into a ConfigMap, and several blocks are reached through a different values path
(`runner_artifacts.plan_artifacts_max_bytes` comes from
`.Values.runners.planArtifactsMaxBytes`) or rendered inside a `with` scope through
a dot-relative reference. Measured across all 30 rendered blocks, this one is the
only block whose fields are all plain, all non-secret and all rendered from the
one obvious path — so here the closure holds with **no exemptions**, which is what
makes it worth asserting as an equality rather than a subset.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

from terrapod.config import ArtifactRetentionConfig

HERE = pathlib.Path(__file__).resolve()

#: A field set this small would mean the model moved and the gate is reading
#: something else. Without it, a renamed model passes on the empty set.
MIN_FIELDS = 10


def _root() -> pathlib.Path | None:
    for cand in HERE.parents:
        if (cand / "helm" / "terrapod" / "Chart.yaml").is_file():
            return cand
    return None


ROOT = _root()
pytestmark = pytest.mark.skipif(ROOT is None, reason="chart not present in this image")


def _model_fields() -> set[str]:
    fields = set(ArtifactRetentionConfig.model_fields)
    assert len(fields) >= MIN_FIELDS, (
        f"only {len(fields)} fields on ArtifactRetentionConfig — the model moved, "
        "so this gate is no longer reading what it claims to"
    )
    return fields


def test_every_retention_setting_has_a_values_entry() -> None:
    """values.yaml is what an operator edits, and what helm-smoke derives from."""
    values = yaml.safe_load((ROOT / "helm/terrapod/values.yaml").read_text())
    block = values["api"]["config"]["artifact_retention"]
    assert set(block) == _model_fields(), (
        "values.yaml's artifact_retention block and ArtifactRetentionConfig disagree.\n"
        f"  missing from values.yaml: {sorted(_model_fields() - set(block))}\n"
        f"  in values.yaml but not the model: {sorted(set(block) - _model_fields())}\n"
        "A field missing here is unreachable: values.schema.json is "
        "additionalProperties: false, so setting it fails `helm lint` outright."
    )


def test_every_retention_setting_is_declared_in_the_schema() -> None:
    """`additionalProperties: false` turns an undeclared key into a hard refusal."""
    schema = json.loads((ROOT / "helm/terrapod/values.schema.json").read_text())
    props = schema["$defs"]["apiConfig"]["properties"]["artifact_retention"]
    assert props.get("additionalProperties") is False, (
        "this gate's premise is that an undeclared key is REFUSED; if "
        "additionalProperties is no longer false, re-derive what a missing "
        "declaration now costs before relaxing anything here"
    )
    assert set(props["properties"]) == _model_fields(), (
        "values.schema.json and ArtifactRetentionConfig disagree.\n"
        f"  undeclared (an operator setting these is REFUSED): "
        f"{sorted(_model_fields() - set(props['properties']))}\n"
        f"  declared but not a model field: "
        f"{sorted(set(props['properties']) - _model_fields())}"
    )


def test_every_retention_setting_has_a_render_line() -> None:
    """The leg that is silently inert when missed: the value never reaches the pod.

    Reads the template source rather than rendered output, because
    `docker/Dockerfile.test` ships the chart and no helm binary — a gate that
    needs `helm template` skips in CI, which is how it becomes decorative.
    """
    tpl = (ROOT / "helm/terrapod/templates/configmap-api.yaml").read_text()
    rendered = set(re.findall(r"\.Values\.api\.config\.artifact_retention\.(\w+)", tpl))
    assert _model_fields() <= rendered, (
        "these settings have no render line in configmap-api.yaml, so an "
        "operator's value never reaches the pod and the code default silently "
        f"wins: {sorted(_model_fields() - rendered)}"
    )
