"""Rule 10 has ONE producer, and nothing may grow a second.

`AGENTS.md` principle 10 is a hard requirement: every datetime the API
serialises is RFC3339 with a trailing `Z`, never `+00:00`, because `go-tfe`
rejects the latter. `api/serialization.rfc3339` has always said in its own
docstring that it is "the one canonical serializer -- prefer it over per-router
`_rfc3339` copies that have drifted (some used bare `.isoformat()` and
regressed the `Z` suffix)".

That preference was enforced by nothing, and by the time it was measured there
were TWENTY-SEVEN per-module copies in seven shapes (#2715). Twenty-three
agreed with the canonical. Four did not:

  - `cloud_identity.py` and `workspace_extensions.py` skipped `astimezone`, so
    a non-UTC or naive value emitted `+05:30` -- no `Z` at all, the exact thing
    rule 10 exists to prevent;
  - `tokens.py` emitted microseconds where the canonical emits whole seconds;
  - `workspace_cost_service.py` did both AND had no `None` guard, so it raised
    an `AttributeError` on a null column where every sibling returned `""`.

Each copy had its own local test asserting its own form, so every copy worked
and every test passed while the surfaces disagreed about the format. That is
the shape a per-module test cannot see and this gate can.

The two absent-value conventions -- `""` and `None` -- are NOT folded. Both are
real wire contracts that consumers read, so `rfc3339` and `rfc3339_or_empty`
are siblings and each module keeps whichever it already served. What is
consolidated is the format.
"""

import ast
import pathlib

import terrapod

ROOT = pathlib.Path(terrapod.__path__[0])

#: The canonical producers. A module-level `_rfc3339` is allowed to EXIST as a
#: local alias -- several modules read better with one -- but its body must do
#: nothing but delegate to one of these.
CANONICAL = {"rfc3339", "rfc3339_or_empty"}


def _timestamp_helpers() -> list[tuple[pathlib.Path, ast.FunctionDef]]:
    out = []
    for path in sorted(ROOT.rglob("*.py")):
        src = path.read_text()
        if "def _rfc3339" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_rfc3339"
            ):
                out.append((path, node))
    return out


def test_no_module_formats_a_timestamp_itself():
    """Every `_rfc3339` delegates; none formats."""
    helpers = _timestamp_helpers()
    # A floor would be blind to the set growing, and growing is the whole defect
    # here -- twenty-seven copies accumulated one at a time. An equality makes a
    # new one visible even when it happens to be correct.
    assert len(helpers) == 27, (
        f"expected 27 modules with a local `_rfc3339` alias, found {len(helpers)}. "
        "If you added or removed one, update this count deliberately -- the point "
        "is that a new one is seen."
    )

    offenders = []
    for path, fn in helpers:
        body = ast.dump(fn)
        # The formatting primitives. Any of these inside a `_rfc3339` means the
        # module is formatting rather than asking.
        for banned in ("strftime", "isoformat"):
            if banned in body:
                offenders.append(f"{path.relative_to(ROOT)}:{fn.lineno} calls {banned}()")
        called = {
            n.func.attr
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        } | {
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if not (called & CANONICAL):
            offenders.append(
                f"{path.relative_to(ROOT)}:{fn.lineno} reaches none of {sorted(CANONICAL)}"
            )

    assert not offenders, (
        "a timestamp is formatted outside the canonical serializer, which is how "
        "the `Z` suffix regressed before (rule 10, #2715):\n  " + "\n  ".join(offenders)
    )


def test_the_canonical_serializers_disagree_only_about_absence():
    """The two differ ONLY in what they return for `None` -- that is the whole
    reason there are two, and a change that made them differ about a real value
    would silently split the format again."""
    import datetime as dt
    from datetime import UTC

    from terrapod.api import serialization

    moment = dt.datetime(2026, 11, 25, 12, 0, 0, 123456, tzinfo=UTC)
    assert serialization.rfc3339(moment) == serialization.rfc3339_or_empty(moment)
    assert serialization.rfc3339(None) is None
    assert serialization.rfc3339_or_empty(None) == ""


def test_the_canonical_form_is_whole_seconds_with_a_z():
    """Pinned explicitly, because this is the property `go-tfe` depends on and
    the one the four divergent copies broke."""
    import datetime as dt
    from datetime import UTC

    from terrapod.api import serialization

    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    # Microseconds dropped, and a non-UTC input converted rather than carried.
    assert serialization.rfc3339(dt.datetime(2026, 11, 25, 12, 0, 0, 999, tzinfo=UTC)) == (
        "2026-11-25T12:00:00Z"
    )
    assert serialization.rfc3339(dt.datetime(2026, 11, 25, 17, 30, 0, tzinfo=ist)) == (
        "2026-11-25T12:00:00Z"
    )
