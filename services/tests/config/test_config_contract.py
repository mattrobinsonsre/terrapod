"""Config-key contract gate (#550) — freeze the operator-settable config surface.

Every leaf field on the Pydantic `Settings` tree is a key an operator can set in
`config.yaml` (rendered by the Helm ConfigMap) or via a `TERRAPOD_*` env var.
Removing or renaming one silently breaks that operator's config on upgrade — the
setting reverts to its code default with no error. This freezes the full dotted
key set and fails CI on any removal/rename; additions are accepted by
regenerating the snapshot.

    UPDATE_API_CONTRACT=1 pytest tests/config/test_config_contract.py
"""

from __future__ import annotations

import json
import os
import types
import typing
from pathlib import Path

from pydantic import BaseModel

from terrapod.config import RunnerConfig, Settings

_SNAPSHOT = Path(__file__).parent / "config_key_contract.json"


def _nested_model(annotation: object) -> type[BaseModel] | None:
    """If the annotation is a (possibly Optional) nested BaseModel, return that
    model class; otherwise None (a leaf, or a list/dict/scalar)."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            if arg is type(None):
                continue
            nested = _nested_model(arg)
            if nested is not None:
                return nested
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _list_item_model(annotation: object) -> type[BaseModel] | None:
    """If the annotation is a (possibly Optional) ``list[Model]``, return the
    item model so per-item operator keys are frozen too — e.g. the ``issuer_url``
    / ``client_id`` sub-keys under ``auth.sso.oidc[]``."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        for arg in typing.get_args(annotation):
            if arg is type(None):
                continue
            item = _list_item_model(arg)
            if item is not None:
                return item
        return None
    if origin is list:
        args = typing.get_args(annotation)
        if args:
            return _nested_model(args[0])
    return None


def _model_key_paths(model: type[BaseModel], prefix: str = "") -> set[str]:
    """Dotted config key paths for one model. Nested models recurse; a
    ``list[Model]`` recurses under a ``[]`` marker; everything else is a leaf."""
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = _nested_model(field.annotation)
        if nested is not None and nested is not model:
            keys |= _model_key_paths(nested, f"{path}.")
            continue
        item_model = _list_item_model(field.annotation)
        if item_model is not None and item_model is not model:
            keys |= _model_key_paths(item_model, f"{path}[].")
            continue
        keys.add(path)
    return keys


def config_key_paths() -> set[str]:
    """The full operator-settable config surface: the API ``Settings`` tree
    (``config.yaml`` / ``TERRAPOD_*``) plus the listener ``RunnerConfig`` tree
    (``runners.yaml``), namespaced so the two files stay distinct."""
    keys = _model_key_paths(Settings)
    keys |= {f"[runners.yaml] {k}" for k in _model_key_paths(RunnerConfig)}
    return keys


def test_config_key_contract_unchanged() -> None:
    current = sorted(config_key_paths())

    if os.environ.get("UPDATE_API_CONTRACT"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n")
        return

    assert _SNAPSHOT.exists(), (
        f"Config key snapshot missing at {_SNAPSHOT}. Generate it with:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/config/test_config_contract.py"
    )
    snapshot = set(json.loads(_SNAPSHOT.read_text()))
    cur = set(current)
    removed = sorted(snapshot - cur)
    added = sorted(cur - snapshot)

    problems: list[str] = []
    if removed:
        problems.append(
            "BREAKING: config keys were REMOVED or RENAMED. An operator's config.yaml "
            "or TERRAPOD_* env silently reverts to the code default on upgrade. This "
            "requires a MAJOR bump or a documented deprecation window — do NOT just "
            "regenerate the snapshot:\n  " + "\n  ".join(removed)
        )
    if added:
        problems.append(
            "New config keys added (additive). Accept them by regenerating:\n"
            "  UPDATE_API_CONTRACT=1 pytest tests/config/test_config_contract.py\n"
            "  " + "\n  ".join(added)
        )
    assert not problems, "\n\n".join(problems)


def test_snapshot_is_substantial() -> None:
    # Bite-check: the config tree is large; a tiny snapshot means the walker broke.
    assert len(config_key_paths()) > 100
