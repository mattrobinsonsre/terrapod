"""Every runner-token auth path checks the run is still live (GHSA-xmrf-hxq9-m59m).

There are FOUR places that turn a `runtok:` credential into an
`AuthenticatedUser`, because three surfaces need their own auth function rather
than the shared dependency: the ordinary dependency and its SSE variant
(`api/dependencies.py`), the package-cache proxy (pip/npm send Basic and Bearer),
and the OCI registry. A check added to only one of them is a check a runner
routes around by asking a different surface — and the OCI path was in fact missed
on the first pass of this fix, and found by writing this gate rather than by
reading.

So this is a source-introspection guard: the invariant is "no function mints a
runner-token principal without consulting the run's liveness", which no
behavioural test can assert about a function nobody has thought to write yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import terrapod

# `terrapod` is an implicit namespace package (no __init__.py) → __file__ is None.
_ROOT = Path(next(iter(terrapod.__path__))).resolve()

#: Files that may construct a runner-token principal. A new one here is not
#: forbidden, but it has to be added deliberately — and then it is held to the
#: same rule as the others.
_AUTH_SOURCES = [
    _ROOT / "api" / "dependencies.py",
    _ROOT / "api" / "routers" / "package_cache.py",
    _ROOT / "services" / "oci" / "auth.py",
]

_MINT = 'auth_method="runner_token"'
#: The CALL, not the name. An earlier version of this gate looked for
#: `is_run_token_usable`, which the function's own `from ... import` line
#: satisfies — so deleting the check and leaving the import passed the gate. That
#: is the defect class this file exists to catch, reproduced in the file that
#: catches it. Matching `await …(` cannot be satisfied by an import, and the
#: refusal below is pinned separately.
#: Two forms, because a caller that holds a session passes it and one that does
#: not uses the helper that opens (and degrades without) its own.
_CHECKS = ("await is_run_token_usable(", "await is_run_token_usable_on_its_own_session(")
#: Likewise: `run_phase=` is the assignment that carries the claim onto the
#: principal, not merely the name of the helper that produced it.
_CLAIMS = "verify_runner_token_claims("


def _functions_with(path: Path, needle: str) -> dict[str, str]:
    """Map function name → source, for every top-level/nested def containing needle.

    Crude on purpose: a real AST walk would be tidier but this reads the file the
    way the reviewer does, and the failure message can quote the function.
    """
    src = path.read_text()
    out: dict[str, str] = {}
    # Split on any `def name(` at any indentation, keeping the name.
    parts = re.split(r"\n(?=\s*(?:async )?def )", src)
    for part in parts:
        m = re.match(r"\s*(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)", part)
        if m and needle in part:
            out[m.group(1)] = part
    return out


def test_every_file_that_mints_a_runner_principal_is_listed() -> None:
    """The allowlist above is only meaningful if nothing else mints one."""
    found = sorted(
        p.relative_to(_ROOT).as_posix() for p in _ROOT.rglob("*.py") if _MINT in p.read_text()
    )
    expected = sorted(p.relative_to(_ROOT).as_posix() for p in _AUTH_SOURCES)
    assert found == expected, (
        "A new runner-token auth path appeared. Add it to _AUTH_SOURCES here and "
        "make sure it calls is_run_token_usable — a surface that skips the check "
        "is a surface a token from a finished run can still use.\n"
        f"found={found}\nlisted={expected}"
    )


def test_every_runner_token_auth_function_checks_run_liveness() -> None:
    offenders: list[str] = []
    for path in _AUTH_SOURCES:
        for name, body in _functions_with(path, _MINT).items():
            if not any(c in body for c in _CHECKS):
                offenders.append(f"{path.relative_to(_ROOT).as_posix()}::{name}")
    assert not offenders, (
        "These functions mint a runner-token principal without calling either of "
        f"{_CHECKS}, so a token whose run has ended still authenticates there "
        f"(GHSA-xmrf-hxq9-m59m): {offenders}"
    )


def test_the_liveness_answer_is_acted_on_not_merely_asked_for() -> None:
    """A call whose result is discarded satisfies the check above and protects
    nothing, so the refusal is pinned separately. Each path refuses in its own
    vocabulary — a 401 here, an `OCIError`/challenge there — so what is required
    is that *something* in the function refuses, named per file."""
    refusals = {
        "api/dependencies.py": "HTTP_401_UNAUTHORIZED",
        "api/routers/package_cache.py": "_unauthorised()",
        "services/oci/auth.py": "UNAUTHORIZED",
    }
    offenders: list[str] = []
    for path in _AUTH_SOURCES:
        rel = path.relative_to(_ROOT).as_posix()
        needle = refusals[rel]
        for name, body in _functions_with(path, _MINT).items():
            if any(c in body for c in _CHECKS) and needle not in body:
                offenders.append(f"{rel}::{name} (expected {needle})")
    assert not offenders, (
        f"These functions ask whether the run is live and then do not refuse: {offenders}"
    )


def test_every_runner_token_auth_function_reads_the_phase_claim() -> None:
    """A path that verifies with the old helper drops `run_phase` to None, which
    every phase-checked endpoint reads as "no claim" — so the phase binding would
    be silently inert for anything authenticated through it."""
    offenders: list[str] = []
    for path in _AUTH_SOURCES:
        for name, body in _functions_with(path, _MINT).items():
            if _CLAIMS not in body or "run_phase=" not in body:
                offenders.append(f"{path.relative_to(_ROOT).as_posix()}::{name}")
    assert not offenders, (
        "These functions mint a runner-token principal without carrying the "
        f"token's phase claim (`{_CLAIMS}` → `run_phase=`), so every phase gate "
        f"silently passes for callers authenticated there: {offenders}"
    )
