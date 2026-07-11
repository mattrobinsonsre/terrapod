"""Helm values-removal contract gate (#550).

`values.schema.json` uses `additionalProperties: false`, so it already blocks
*adding* an undeclared value key — but it does NOT stop a key being *removed*.
An operator's `values.yaml` / release overrides depend on the keys the chart
declares; silently dropping or renaming one breaks their deployment on upgrade
(the override now targets a key that no longer exists, or a default they relied on
vanishes). That's a breaking change to the Helm-values surface.

This gate freezes the set of dotted key paths in `values.yaml` and diffs it
against a committed snapshot. Removing/renaming a key is **breaking** (MAJOR bump
or a documented deprecation, not a regen); adding one is additive (regenerate with
`UPDATE_API_CONTRACT=1 pytest tests/helm/test_values_contract.py`). It complements
the schema gate: schema blocks adds-without-declaration, this blocks removes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

# The chart is copied into the test image at /app/helm (see docker/Dockerfile.test).
_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback (running outside the image)
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_VALUES = _HELM_ROOT / "values.yaml"
_SNAPSHOT = Path(__file__).parent / "helm_values_contract.json"


def _flatten(obj: object, prefix: str = "") -> set[str]:
    """Dotted key paths for every declared value.

    Non-empty maps recurse; scalars, empty maps, lists (not indexed), and null are
    leaves recorded at their path. So `api.podAnnotations: {}` records
    `api.podAnnotations` (a declared, operator-fillable key) while
    `api.resources.requests.cpu: "1"` records the full path.
    """
    if isinstance(obj, dict) and obj:
        keys: set[str] = set()
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            keys |= _flatten(v, path)
        return keys
    return {prefix} if prefix else set()


def values_keys() -> list[str]:
    data = yaml.safe_load(_VALUES.read_text())
    return sorted(_flatten(data))


def test_values_keys_unchanged() -> None:
    current = values_keys()

    if os.environ.get("UPDATE_API_CONTRACT"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n")
        return

    assert _SNAPSHOT.exists(), (
        f"Helm values snapshot missing at {_SNAPSHOT}. Generate it with:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/helm/test_values_contract.py"
    )
    snapshot: list[str] = json.loads(_SNAPSHOT.read_text())

    removed = sorted(set(snapshot) - set(current))
    added = sorted(set(current) - set(snapshot))

    problems: list[str] = []
    if removed:
        problems.append(
            f"BREAKING: values.yaml keys removed/renamed: {removed}. Operators' "
            "overrides depend on these — a MAJOR bump or a documented deprecation "
            "window is required, NOT a snapshot regen. (If this is a rename, the old "
            "key must stay through the deprecation window.)"
        )
    if added:
        problems.append(
            "New values.yaml keys added (additive). Regenerate the snapshot:\n"
            "  UPDATE_API_CONTRACT=1 pytest tests/helm/test_values_contract.py\n"
            f"  added: {added}"
        )
    assert not problems, "\n\n".join(problems)


def test_values_file_parses_nonempty() -> None:
    # Bite-check: a silently-empty parse would disable the gate.
    assert len(values_keys()) > 50
