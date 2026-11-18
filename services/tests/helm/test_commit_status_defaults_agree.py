"""The commit-status defaults live in three places: they must stay equal.

values.yaml, the `dig` fallbacks in configmap-api.yaml, and `CommitStatusConfig`.
The generic defaults test covers values.yaml against the code; the template
fallbacks are read here as text, because the unit tier has no helm binary.
"""

import re
from pathlib import Path

import yaml

from terrapod.config import CommitStatusConfig

_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback (running outside the image)
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_KEYS = ("enabled", "plan_name", "policy_name", "apply_name", "per_workspace")


def _code() -> dict:
    return {k: getattr(CommitStatusConfig(), k) for k in _KEYS}


def _values() -> dict:
    values = yaml.safe_load((_HELM_ROOT / "values.yaml").read_text())
    return values["api"]["config"]["vcs"]["commit_statuses"]


def _template_fallbacks() -> dict:
    text = (_HELM_ROOT / "templates" / "configmap-api.yaml").read_text()
    out = {}
    for key in _KEYS:
        m = re.search(rf'dig "commit_statuses" "{key}" (".*?"|true|false) ', text)
        assert m, f"configmap-api.yaml has no dig fallback for commit_statuses.{key}"
        out[key] = yaml.safe_load(m.group(1))
    return out


def test_values_yaml_matches_the_code_defaults():
    assert _values() == _code()


def test_template_fallbacks_match_the_code_defaults():
    assert _template_fallbacks() == _code()


def test_every_key_the_model_has_is_in_all_three_places():
    assert set(CommitStatusConfig.model_fields) == set(_KEYS)
    assert set(_values()) == set(_KEYS)
