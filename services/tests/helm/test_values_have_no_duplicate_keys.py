"""No values file declares the same key twice (#1572).

YAML resolves a duplicate mapping key by taking the last one and discarding the
first, **silently**. So appending a block to a values file — the obvious way to
add a setting — can delete an unrelated block that happened to share its
top-level key, and nothing says so: the file parses, `helm lint` passes, and
`helm template` renders a chart missing settings nobody removed.

That is not hypothetical. Adding an `api.config.engines` block to
`values-eval.yaml` by appending

    api:
      config:
        engines: ...

left two top-level `api:` keys, and the second one discarded `replicas`,
`strategy` and every `config` setting the first declared. The eval stack then
booted without them and failed its own smoke test with "seeded sample workspace
not found" — a message pointing nowhere near the cause.

`yaml.safe_load` cannot catch this, because by the time it returns the duplicate
is already resolved. The check has to run on the parse events, which is what
the composer below does.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

# The test image copies the chart to /app/helm; a local checkout has it three
# levels up from here. Same resolution as the other tests in this directory —
# `parents[3]` alone silently becomes `/helm/terrapod` inside the image, and an
# empty glob then reports every profile as clean.
CHART = pathlib.Path("/app/helm/terrapod")
if not CHART.is_dir():
    CHART = pathlib.Path(__file__).resolve().parents[3] / "helm" / "terrapod"
VALUES = sorted(CHART.glob("values*.yaml"))


def _duplicate_keys(path: pathlib.Path) -> list[str]:
    """Every key declared more than once in the same mapping, as a dotted path.

    Walks the composed node tree rather than the loaded dict: the loader has
    already thrown the collision away.
    """
    found: list[str] = []

    def walk(node, prefix: str = "") -> None:
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key_node, value_node in node.value:
                key = str(key_node.value)
                dotted = f"{prefix}{key}"
                if key in seen:
                    found.append(f"{dotted} (line {key_node.start_mark.line + 1})")
                seen.add(key)
                walk(value_node, f"{dotted}.")
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                walk(item, f"{prefix}[{i}].")

    with path.open(encoding="utf-8") as fh:
        root = yaml.compose(fh)
    if root is not None:
        walk(root)
    return found


@pytest.mark.parametrize("path", VALUES, ids=lambda p: p.name)
def test_no_key_is_declared_twice(path: pathlib.Path):
    dupes = _duplicate_keys(path)
    assert not dupes, (
        f"{path.name} declares these keys more than once, and YAML silently "
        f"keeps only the LAST — everything the earlier one set is discarded "
        f"with no error anywhere:\n  " + "\n  ".join(dupes)
    )


def test_every_profile_is_covered():
    """The three profiles are a stated contract; a fourth appearing unchecked
    would be the same gap one level up."""
    names = {p.name for p in VALUES}
    assert {"values.yaml", "values-local.yaml", "values-eval.yaml"} <= names, names


def test_the_check_would_actually_catch_one(tmp_path: pathlib.Path):
    """Mutation-proof, because a detector that never fires is indistinguishable
    from a file that is clean."""
    f = tmp_path / "values-dupe.yaml"
    f.write_text("api:\n  replicas: 1\napi:\n  config:\n    x: 1\n", encoding="utf-8")
    assert _duplicate_keys(f), "the duplicate-key detector did not fire on a duplicate"

    nested = tmp_path / "values-nested.yaml"
    nested.write_text("api:\n  config:\n    a: 1\n    a: 2\n", encoding="utf-8")
    assert any("api.config.a" in d for d in _duplicate_keys(nested))

    clean = tmp_path / "values-clean.yaml"
    clean.write_text("api:\n  replicas: 1\n  config:\n    x: 1\n", encoding="utf-8")
    assert not _duplicate_keys(clean), "a clean file was reported as having duplicates"
