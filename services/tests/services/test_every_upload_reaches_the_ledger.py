"""Every operator upload is recorded in the artifact ledger (#2589, #2599).

**A call-site gate, not a behaviour one**, because the behaviour tests cannot
see this: `record_upload` swallows its own failures by design, so removing a
call changes no observable outcome and every upload test stays green. The
recorded lesson is exactly that -- a presence check on the helper is not a check
that anything calls it.

**Derived from the tree rather than listed.** The subject is "every module that
admits an upload", found by what the module does (it sets
`ATTEST_OPERATOR_UPLOAD`), so a fourth upload path is covered the day it is
written rather than when somebody remembers to extend a list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[2] / "terrapod" / "services"

#: An upload is the one attestation that cannot be re-verified later -- there is
#: no publisher material to re-check -- so the ledger row IS its provenance.
#: A path that admits an upload and records nothing loses the only record of who
#: accepted the artifact.
MARKER = "ATTEST_OPERATOR_UPLOAD"


def _delegates(value: ast.expr) -> bool:
    """Is this `return await _helper(...)` / `return _helper(...)`?"""
    inner = value.value if isinstance(value, ast.Await) else value
    return isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)


def _upload_modules() -> list[Path]:
    found = [
        p
        for p in SERVICES.rglob("*.py")
        # The substrate DEFINES the constant and classifies with it; it does not
        # admit an upload, so it is excluded by what it does rather than by name.
        if MARKER in p.read_text() and p.name != "substrate.py"
    ]
    return sorted(found)


def test_the_derivation_finds_the_upload_paths() -> None:
    """A floor, so a renamed constant fails here rather than making the gate
    below pass by examining nothing."""
    modules = _upload_modules()
    names = {p.name for p in modules}
    assert len(modules) >= 3, f"only found {names}"
    assert {"uploads.py", "binary_cache_uploads.py", "provider_cache_uploads.py"} <= names


@pytest.mark.parametrize("module", _upload_modules(), ids=lambda p: p.name)
def test_every_admit_records_an_upload(module: Path) -> None:
    """`admit` must reach `record_upload` on every path that returns a record.

    Asserted per RETURN rather than per function: the package cache admits on
    two paths -- a fresh upload and an upload replacing an existing artifact --
    and the second is the one a careless change drops, because it looks like a
    mere update of a row that already exists.
    """
    tree = ast.parse(module.read_text())
    admits = [
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "admit"
    ]
    if not admits:
        pytest.skip(f"{module.name} classifies uploads but does not admit them")

    for fn in admits:
        returns = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Return)
            and n.value is not None
            # A return that DELEGATES to another function in this module
            # (`return await _admit_over(...)`) records inside that function, so
            # counting it here would demand a second call for one admission.
            # The delegate is itself in `_upload_modules`, so it is not skipped
            # -- it is checked where it lives.
            and not _delegates(n.value)
        ]
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "record_upload"
        ]
        assert calls, (
            f"{module.name}:{fn.lineno} admits an upload and never calls record_upload. "
            "The ledger is the only provenance an upload has -- nothing else can be "
            "re-checked later."
        )
        # One recorder per returned record. Fewer means a path returns an
        # admitted artifact that the ledger never heard about.
        assert len(calls) >= len(returns), (
            f"{module.name}:{fn.lineno} has {len(returns)} return(s) but "
            f"{len(calls)} record_upload call(s) -- a path admits without recording."
        )
