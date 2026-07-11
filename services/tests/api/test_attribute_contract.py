"""API attribute contract gate (#550) — freeze JSON:API response attribute names.

The single most common silent break: dropping or renaming a `data.attributes`
key breaks every consumer that reads it (go-terrapod, the Terraform provider,
the web UI, third-party automation) even though the *route* is unchanged and no
error is raised — the field just vanishes. The existing `test_api_sdk_contract`
guards only the `workspace` resource; this gate covers **every** JSON:API
serializer.

It AST-parses each router, finds every serializer's inline `"attributes": {...}`
block, and diffs the attribute-name set per serializer against a committed
snapshot (`api_attribute_contract.json`):

- a **removed / renamed** attribute is a **breaking change** — MAJOR bump or a
  documented deprecation window, NOT a snapshot regen;
- a **new** attribute is additive — accept it by regenerating
  (`UPDATE_API_CONTRACT=1 pytest tests/api/test_attribute_contract.py`).

Scope/limits: only statically-declared inline `"attributes": {...}` keys are
frozen. Attributes added conditionally after the dict literal (rare) are not yet
covered — a known gap, never a false failure.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import terrapod.api.routers as _routers_pkg

_ROUTERS_DIR = Path(_routers_pkg.__file__).resolve().parent
_SNAPSHOT = Path(__file__).parent / "api_attribute_contract.json"


def _attributes_of_function(fn: ast.AST) -> set[str] | None:
    """Return the string keys of the first inline ``"attributes": {...}`` dict in
    a function, or None if it isn't a JSON:API serializer."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "attributes"
                and isinstance(value, ast.Dict)
            ):
                return {
                    k.value
                    for k in value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    return None


def extract_attribute_contract() -> dict[str, list[str]]:
    """`{"<router>.<serializer>": [sorted attribute names]}` for every JSON:API
    serializer across the routers — discovered by structure (a function whose
    body builds an ``"attributes"`` dict), not by name."""
    contract: dict[str, list[str]] = {}
    for path in sorted(_ROUTERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            attrs = _attributes_of_function(node)
            if attrs:
                contract[f"{path.stem}.{node.name}"] = sorted(attrs)
    return contract


def test_attribute_contract_unchanged() -> None:
    current = extract_attribute_contract()

    if os.environ.get("UPDATE_API_CONTRACT"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        return

    assert _SNAPSHOT.exists(), (
        f"Attribute contract snapshot missing at {_SNAPSHOT}. Generate it with:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/api/test_attribute_contract.py"
    )
    snapshot: dict[str, list[str]] = json.loads(_SNAPSHOT.read_text())

    breaking: list[str] = []
    additive: list[str] = []

    for serializer, snap_attrs in snapshot.items():
        if serializer not in current:
            breaking.append(f"{serializer}: entire serializer removed/renamed")
            continue
        removed = sorted(set(snap_attrs) - set(current[serializer]))
        if removed:
            breaking.append(f"{serializer}: removed/renamed attributes {removed}")
    for serializer, cur_attrs in current.items():
        added = sorted(set(cur_attrs) - set(snapshot.get(serializer, [])))
        if added:
            additive.append(f"{serializer}: added {added}")

    problems: list[str] = []
    if breaking:
        problems.append(
            "BREAKING: JSON:API response attributes were REMOVED or RENAMED. Every "
            "consumer that reads them (go-terrapod, the provider, the web UI, "
            "third-party automation) breaks silently. This requires a MAJOR bump or "
            "a documented deprecation window — do NOT just regenerate the snapshot:\n"
            "  " + "\n  ".join(breaking)
        )
    if additive:
        problems.append(
            "New attributes were added (additive, non-breaking). Accept them by "
            "regenerating the snapshot:\n"
            "  UPDATE_API_CONTRACT=1 pytest tests/api/test_attribute_contract.py\n"
            "  " + "\n  ".join(additive)
        )
    assert not problems, "\n\n".join(problems)


def test_extractor_finds_the_known_serializers() -> None:
    # Bite-check: the extractor must actually resolve real serializers (guards
    # against a silently-empty contract that would disable the gate).
    contract = extract_attribute_contract()
    assert len(contract) >= 25
    assert "variables._var_json" in contract
    assert "key" in contract["variables._var_json"]
    assert "runs._run_json" in contract
    assert "status" in contract["runs._run_json"]
