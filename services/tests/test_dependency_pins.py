"""Guard rails on dependency pins that have bitten us before.

These read pyproject.toml and assert structural invariants about a few
dependencies whose unpinned/under-pinned state has caused (or could
cause) silent framework-level breakage. They are deliberately cheap
source-introspection checks — no install, no import.
"""

from __future__ import annotations

import pathlib
import tomllib

_PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def _deps() -> dict[str, object]:
    data = tomllib.loads(_PYPROJECT.read_text())
    return data["tool"]["poetry"]["dependencies"]


def _version_spec(spec: object) -> str:
    # A dependency value is either a bare version string or a table with a
    # `version` key (e.g. uvicorn = {extras = [...], version = "..."}).
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return str(spec.get("version", ""))
    return ""


def test_starlette_is_directly_pinned_with_upper_bound() -> None:
    """starlette MUST be a direct dependency with an explicit upper bound.

    fastapi only declares a floor (``starlette>=0.46``, no ceiling), so
    without our own cap starlette floats to the latest release on every
    build — a starlette major/minor can then change framework behaviour
    with no deliberate bump on our side, and a fastapi bump can drag a new
    starlette in silently. Pinning it directly forces every starlette move
    to be an explicit, reviewed edit; a fastapi version needing a starlette
    outside our range then fails to resolve instead of swapping it quietly.
    (fastapi 0.137 + starlette 1.x is exactly the trap this guards.)

    If you are intentionally taking a new starlette, bump the pin in
    pyproject.toml — do not delete the upper bound.
    """
    deps = _deps()
    assert "starlette" in deps, (
        "starlette must be declared as a DIRECT dependency in pyproject.toml, "
        "not left as a floor-only transitive of fastapi"
    )
    spec = _version_spec(deps["starlette"])
    assert "<" in spec, f"starlette needs an explicit upper bound; got {spec!r}"


def test_fastapi_has_upper_bound() -> None:
    """fastapi must keep an explicit upper bound (it ships breaking changes
    in 0.x minors — e.g. the 0.137 include_router refactor)."""
    spec = _version_spec(_deps()["fastapi"])
    assert "<" in spec, f"fastapi needs an explicit upper bound; got {spec!r}"
