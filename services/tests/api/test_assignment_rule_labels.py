"""Every assignment-rule dimension an operator can STORE has a human label.

The blast-radius screen (`/admin/variable-sets/<id>`) is the one view that answers
"who receives this credential", and `AssignmentRuleSummary`'s own docstring promises
"never a serialised object, which is what the raw shape would leak". It leaked twice
(#2621): `exclude` fell through to `String(value)` and rendered `[object Object]`
under the bare untranslated term `exclude`, and `engine_version` -- the canonical
spelling, and the one the API stores whenever a caller sends either -- had no label
at all, so every rule written with it showed a bare key as its term.

`AssignmentRule` is `Record<string, unknown>` on the web side, so TypeScript
guarantees nothing here and a new dimension arrives unlabelled in silence.

WHAT IS REACHABLE IS DERIVED BY DRIVING THE ROUTER, not written down. A dimension
the write path refuses cannot reach the screen, so requiring a label for it would be
labelling the unreachable -- and a hand-kept list of "the refused ones" is the ledger
this avoids. `all`, `autodiscovery_rule_id` and `workspace_ids` are refused today and
are excluded by that measurement rather than by a name.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from fastapi import HTTPException

from terrapod.api.routers.variables import _validated_assignment_rule
from terrapod.services.workspace_search_service import WorkspaceFilter

_TESTS_DIR = pathlib.Path(__file__).resolve().parent


def _surface(relative: str) -> pathlib.Path:
    """A non-Python surface, resolved in the local checkout and in the test image.

    The image flattens `services/` into `/app`, so the tree sits one directory
    shallower there; counting parents gets exactly one of the two layouts right.
    It REFUSES rather than skipping, because a gate that cannot find the file it
    reads would otherwise pass vacuously.
    """
    for base in (_TESTS_DIR.parents[2], _TESTS_DIR.parents[1]):
        candidate = base / relative
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Cannot find {relative} from {_TESTS_DIR}. If this is the test image, the "
        "file needs a COPY line in docker/Dockerfile.test — this gate reads it, so "
        "without it the gate would pass vacuously."
    )


_COMPONENT = _surface("web/src/components/assignment-rule-editor.tsx")


def _labelled() -> set[str]:
    """The keys `AssignmentRuleSummary`'s LABELS map renders a human term for."""
    src = _COMPONENT.read_text()
    block = re.search(r"const LABELS[^=]*=\s*\{(.*?)\n  \}", src, re.S)
    assert block, (
        "cannot find the LABELS map in AssignmentRuleSummary — teach this reader its "
        "new shape rather than letting the gate match nothing"
    )
    keys = set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))
    assert len(keys) >= 13, f"only {len(keys)} label(s) read; the matcher stopped matching"
    return keys


def _a_value_that_parses(dim: str) -> object:
    """A value the selector would parse, so a refusal is the only thing that can refuse.

    Derived from the annotation and the name, so the next boolean or id dimension
    needs no edit here.
    """
    annotation = str(WorkspaceFilter.model_fields[dim].annotation)
    if "bool" in annotation:
        return True
    if dim.endswith("_id"):
        return "00000000-0000-4000-8000-000000000000"
    if dim == "workspace_ids":
        return ["00000000-0000-4000-8000-000000000000"]
    if dim == "exclude":
        return [{"labels": {"a": "b"}}]
    if dim == "labels":
        return {"t": "x"}
    return "x"


def _storable() -> set[str]:
    """Dimensions the write path accepts, measured by driving it."""
    out = set()
    for dim in WorkspaceFilter.model_fields:
        rule = {dim: _a_value_that_parses(dim)}
        if dim != "labels":
            # `exclude` alone is not a rule, and a bare selector needs a positive
            # term beside it on some paths; `labels` makes every shape a real rule.
            rule["labels"] = {"t": "x"}
        try:
            _validated_assignment_rule({"assignment-rule": rule})
        except HTTPException:
            continue
        out.add(dim)
    return out


def test_the_measurement_itself_is_not_empty() -> None:
    """Both halves, or the comparison below is vacuous in either direction."""
    storable = _storable()
    assert len(storable) >= 10, f"only {len(storable)} storable dimension(s): {sorted(storable)}"
    refused = set(WorkspaceFilter.model_fields) - storable
    assert refused, "nothing is refused, so the derived boundary is doing no work"


def test_every_storable_dimension_has_a_human_label() -> None:
    missing = sorted(_storable() - _labelled())
    assert not missing, (
        "these can be stored in an assignment rule and render as a bare untranslated "
        f"key on the blast-radius screen: {missing}"
    )


def test_both_spellings_of_the_version_selector_are_labelled() -> None:
    """One selector, two names (#1559), and the API stores whichever was sent.

    Pinned separately because only one of them is in `model_fields`: the legacy
    `terraform_version` is an alias the canonical-form normaliser folds, so the
    derived test above cannot see it, and it was the ONLY one labelled.
    """
    labelled = _labelled()
    for spelling in ("engine_version", "terraform_version"):
        assert spelling in labelled, f"{spelling} renders as a bare key"


@pytest.mark.parametrize("dim", ["labels", "exclude"])
def test_a_dimension_whose_value_is_not_a_primitive_is_rendered_explicitly(dim: str) -> None:
    """`String(value)` on an object gives `[object Object]`, which is the leak.

    Asserted on the two dimensions whose value is a container today. A new one
    would be caught by the label test above only if it were also unlabelled, so
    this is the narrower half and says so.
    """
    src = _COMPONENT.read_text()
    branch = re.search(rf"key === '{dim}'", src)
    assert branch, (
        f"`{dim}` holds a container, so it needs its own render branch; without one it "
        "falls through to `String(value)` and reads `[object Object]`"
    )
    fallthrough = src.index("{String(value)}")
    assert branch.start() < fallthrough, (
        f"the `{dim}` branch is after the `String(value)` fallthrough, so it never runs"
    )
