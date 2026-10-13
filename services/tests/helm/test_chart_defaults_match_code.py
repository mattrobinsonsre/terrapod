"""The chart's default and the code's default for one setting must be the same.

Every `api.config.*` setting has **two** defaults: the one `Settings` declares
and the one `values.yaml` ships. Three gates already compare the two surfaces and
none of them compares the values. `test_values_contract` snapshots the keys in
values.yaml; `test_config_contract` snapshots the keys `Settings` accepts;
`test_rendered_config_matches_settings` asks whether every rendered leaf has
somewhere to go. So two numbers can disagree for a year and everything stays
green.

They did. `registry.platform_tools` shipped opa **1.19.0**, trivy **0.72.0** and
checkov **3.3.8** in `config.py` against the chart's **1.21.1**, **0.75.0** and
**3.3.26** — because the release ritual that reviews those versions every minor
updates the chart, and nothing has ever sent anyone to `config.py`. And
`ai_summary.plan_json_max_bytes` was born disagreeing: one commit introduced
600_000 in the code and 500_000 in the chart, and the docs ended up explaining
the discrepancy in three places rather than anyone reconciling it. Both are
500_000 now — the chart's number, because that is what every deployment that
does not override it actually gets.

**Why a disagreement is reachable rather than academic.** `configmap-api.yaml`
renders most keys behind an `if`, so a value the operator blanks is simply
absent from the ConfigMap and the **code** default applies. The chart's number is
also the one every reader believes, because values.yaml is the documented
surface. So the two defaults differing means the deployment can take a value
nobody intended and no document states.

Deliberate differences exist and are listed below with their reasons. The list
is itself checked, because an exemption ledger a gate trusts is the next place a
bug hides: an entry naming a path that no longer differs is stale and fails, and
so is one naming a path that is not in both surfaces.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from terrapod.config import Settings

# The chart is copied into the test image at /app/helm (see
# docker/Dockerfile.test), where `services/` is flattened to `/app` — so
# parents[3] resolves to `/` in the container and only works in a checkout.
# Same idiom as test_values_contract.py; getting it wrong errors at COLLECTION,
# because the parametrize below reads the file at import time.
_HELM_ROOT = Path("/app/helm/terrapod")
if not _HELM_ROOT.exists():  # local checkout fallback (running outside the image)
    _HELM_ROOT = Path(__file__).resolve().parents[3] / "helm" / "terrapod"

_VALUES = _HELM_ROOT / "values.yaml"

#: Settings whose two defaults differ ON PURPOSE, each with the reason. Adding an
#: entry is a claim that the difference is intended — not a way past the gate.
DELIBERATELY_DIFFERENT: dict[str, str] = {
    # The code cannot pick a model id: an empty value is how "no model
    # configured" is expressed, and the AI features are off until one is set.
    # The chart offers a working default so the features are usable out of the
    # box on a deployment that has Bedrock access.
    "ai_summary.model": "'' in code means the feature is off; the chart offers a usable model",
    "ai_onboarding.model": "'' in code means the feature is off; the chart offers a usable model",
    "ai_architecture.model": "'' in code means the feature is off; the chart offers a usable model",
    # Inverted on purpose: a localhost default is a convenience for a local
    # process and actively wrong inside a cluster, where the operator must give
    # a reachable URL. So the chart ships empty and the code ships localhost.
    "auth.callback_base_url": "localhost suits a local process; in-cluster the operator must set it",
    "storage.filesystem.base_url": (
        "localhost suits a local process; in-cluster the operator must set it"
    ),
    # Case only, and the consumer is case-insensitive —
    # `logging_config.configure_logging` does `getattr(logging, log_level.upper())`.
    "log_level": "case only; the consumer upper-cases it before use",
}

#: A floor, so a refactor that stops flattening either side fails loudly rather
#: than comparing nothing and passing. Measured at 257 comparable leaves.
MIN_COMPARABLE = 200


def _flatten_model(model: type[BaseModel], prefix: str = "") -> dict[str, Any]:
    """A pydantic model's declared defaults, as dotted paths.

    Sub-models are recursed through whether they are annotated directly or
    produced by a `default_factory`, which is how most of `Settings` is built.
    A required field has no default to compare, so it is skipped.
    """
    out: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.update(_flatten_model(annotation, f"{path}."))
            continue
        if field.default_factory is not None:
            try:
                made = field.default_factory()  # type: ignore[call-arg]
            except Exception:
                continue
            if isinstance(made, BaseModel):
                out.update(_flatten_model(type(made), f"{path}."))
            else:
                out[path] = made
            continue
        if field.is_required():
            continue
        out[path] = field.default
    return out


def _flatten_tree(tree: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(tree, dict) and tree:
        for key, value in tree.items():
            out.update(_flatten_tree(value, f"{prefix}{key}."))
    else:
        out[prefix.rstrip(".")] = tree
    return out


@lru_cache(maxsize=1)
def _code_defaults() -> dict[str, Any]:
    return _flatten_model(Settings)


@lru_cache(maxsize=1)
def _chart_defaults() -> dict[str, Any]:
    values = yaml.safe_load(_VALUES.read_text())
    return _flatten_tree(values["api"]["config"])


@lru_cache(maxsize=1)
def _comparable() -> tuple[str, ...]:
    code, chart = _code_defaults(), _chart_defaults()
    return tuple(sorted(set(code) & set(chart)))


class TestChartDefaultsMatchCode:
    @pytest.mark.parametrize("setting", _comparable())
    def test_the_two_defaults_agree(self, setting: str) -> None:
        code = _code_defaults()[setting]
        chart = _chart_defaults()[setting]
        if setting in DELIBERATELY_DIFFERENT:
            pytest.skip(f"deliberate: {DELIBERATELY_DIFFERENT[setting]}")
        assert code == chart, (
            f"api.config.{setting} defaults to {chart!r} in helm/terrapod/values.yaml "
            f"and {code!r} in config.py.\n\n"
            f"Most keys render behind an `if`, so an operator who blanks this one gets "
            f"the config.py value while every reader of values.yaml believes the other. "
            f"Update BOTH, or — if the difference is intended — add it to "
            f"DELIBERATELY_DIFFERENT in this file with the reason."
        )

    def test_the_gate_compares_enough_to_be_meaningful(self) -> None:
        comparable = _comparable()
        assert len(comparable) >= MIN_COMPARABLE, (
            f"only {len(comparable)} settings resolved on both sides, expected at least "
            f"{MIN_COMPARABLE}. Either the config models moved or the flattening broke — "
            f"this gate must not pass by comparing almost nothing."
        )

    def test_the_platform_tool_versions_are_among_them(self) -> None:
        """The drift that prompted this gate, named explicitly.

        A general gate can stop covering a specific thing without failing, and
        these three are the ones a release ritual touches by hand every minor.
        """
        comparable = set(_comparable())
        expected = {
            "registry.platform_tools.opa_version",
            "registry.platform_tools.trivy_version",
            "registry.platform_tools.checkov_version",
        }
        missing = sorted(expected - comparable)
        assert not missing, (
            f"{missing} are not being compared. These are the versions the per-minor "
            f"platform-tool review updates in values.yaml, and the drift this gate "
            f"exists for — they must stay in scope."
        )


class TestTheExemptionLedgerIsItselfChecked:
    """An entry is a claim about reality, so both halves are verified."""

    @pytest.mark.parametrize("setting", sorted(DELIBERATELY_DIFFERENT))
    def test_each_exemption_names_a_setting_on_both_surfaces(self, setting: str) -> None:
        assert setting in _code_defaults(), (
            f"DELIBERATELY_DIFFERENT names {setting!r}, which no Settings field declares "
            f"a default for. A renamed or removed setting leaves a dead exemption behind."
        )
        assert setting in _chart_defaults(), (
            f"DELIBERATELY_DIFFERENT names {setting!r}, which values.yaml does not set "
            f"under api.config. A dead exemption hides the next real difference."
        )

    @pytest.mark.parametrize("setting", sorted(DELIBERATELY_DIFFERENT))
    def test_each_exemption_still_describes_a_difference(self, setting: str) -> None:
        code = _code_defaults().get(setting)
        chart = _chart_defaults().get(setting)
        assert code != chart, (
            f"DELIBERATELY_DIFFERENT still lists {setting!r}, but both sides now agree "
            f"on {code!r}. Remove the entry — a ledger of things that are no longer true "
            f"is how a gate quietly stops gating."
        )
