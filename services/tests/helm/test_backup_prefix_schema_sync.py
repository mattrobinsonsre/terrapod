"""`backup.prefix` in values.schema.json must refuse what `shared_prefix` refuses.

Helm lints with the schema and the backup job validates with `shared_prefix`; without
this, a bad prefix installs cleanly and the job fails at its next scheduled run.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from terrapod.storage.keys import WORKSPACE_KEY_PREFIXES, shared_prefix

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback (running outside the image)
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"


def _schema_accepts(prefix: str) -> bool:
    schema = json.loads((_HELM_ROOT / "values.schema.json").read_text())
    return jsonschema.Draft7Validator(
        schema["properties"]["backup"]["properties"]["prefix"]
    ).is_valid(prefix)


def _model_accepts(prefix: str) -> bool:
    try:
        shared_prefix("backup.prefix", prefix)
    except ValueError:
        return False
    return True


_REFUSED = [
    spelling
    for p in WORKSPACE_KEY_PREFIXES
    for spelling in (p, p + "db/", p.rstrip("/"), "./" + p, "././" + p.rstrip("/"))
]
_ACCEPTED = ["backups/", "backups", "dr/state/", "statements/", "./backups/", ""]


@pytest.mark.parametrize("prefix", _REFUSED)
def test_both_refuse_a_workspace_scoped_prefix(prefix: str) -> None:
    assert not _schema_accepts(prefix)
    assert not _model_accepts(prefix)


@pytest.mark.parametrize("prefix", _ACCEPTED)
def test_both_accept_a_shared_prefix(prefix: str) -> None:
    assert _schema_accepts(prefix)
    assert _model_accepts(prefix)
