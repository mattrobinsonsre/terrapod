"""Every committed recipe parses and conforms to the shape the engine requires.

The cost-contract tests (`services/tests/services/test_cost_pricegen_contract.py`)
pin the *engine's* output against hand-curated rows, but nothing loaded the actual
`pricegen/providers/*/recipes/*.yaml` files — so a typo'd field or a malformed
recipe passed CI and only failed later in the scheduled live-cloud pricesheet
build. This test globs every real recipe (and `recipes.yaml`) and asserts each is
valid YAML with the keys `generate.py` / `engine.generate` read, so a broken
recipe fails at PR time instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_PRICEGEN = Path(__file__).resolve().parents[1]
_RECIPE_FILES = sorted(_PRICEGEN.glob("providers/*/recipes/*.yaml"))


def test_there_are_recipes_to_check():
    # Guard against a glob that silently matches nothing (e.g. a layout move) —
    # a green "0 recipes checked" would be a false pass.
    assert len(_RECIPE_FILES) >= 20, f"expected the recipe corpus, found {len(_RECIPE_FILES)}"


@pytest.mark.parametrize("recipe_path", _RECIPE_FILES, ids=lambda p: f"{p.parent.parent.name}/{p.stem}")
def test_recipe_parses_and_has_required_shape(recipe_path: Path):
    doc = yaml.safe_load(recipe_path.read_text())
    assert isinstance(doc, dict), f"{recipe_path.name}: top level is not a mapping"
    # Keys generate.py reads unconditionally.
    assert doc.get("resource_type"), f"{recipe_path.name}: missing resource_type"
    assert doc.get("service"), f"{recipe_path.name}: missing service"
    # A recipe is either component-based (AWS/Azure direct match) or computed
    # (GCP arithmetic assembly) — engine.generate vs generate_computed.
    has_components = isinstance(doc.get("components"), list) and doc["components"]
    has_computed = "computed" in doc
    assert has_components or has_computed, (
        f"{recipe_path.name}: needs a non-empty `components:` list or a `computed:` block"
    )
    # The file stem should match its declared resource_type (how generate.py
    # resolves `--recipe <name>` → the file), catching a mis-named file.
    assert recipe_path.stem == doc["resource_type"], (
        f"{recipe_path.name}: stem != resource_type ({doc['resource_type']})"
    )


def test_recipes_yaml_index_parses():
    doc = yaml.safe_load((_PRICEGEN / "recipes.yaml").read_text())
    assert isinstance(doc.get("recipes"), list) and doc["recipes"], "recipes.yaml: no recipes list"
    # regions is a per-provider map: {aws: [...], azure: [...], gcp: [...]}.
    regions = doc.get("regions")
    assert isinstance(regions, dict) and regions, "recipes.yaml: no regions map"
    assert all(isinstance(v, list) and v for v in regions.values()), "recipes.yaml: empty region list"
    # Every recipes[] entry names a provider + recipe + offer.
    for r in doc["recipes"]:
        assert r.get("provider") and r.get("recipe"), f"recipes.yaml entry missing provider/recipe: {r}"
