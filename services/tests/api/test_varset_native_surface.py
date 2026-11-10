"""The variable-set routes have a native home, and it is the same handler (#2469).

Variable sets were reachable only through the TFE-compatibility surface. That
surface is engine-filtered and its serialiser strips native-only attributes from
its responses, so a Terrapod-native consumer -- the Terraform provider, through
go-terrapod -- could not read back what it had just written. The fix is a native
door, not a second implementation.

These assert the MOUNT rather than the behaviour, deliberately. What the
handlers do is already covered by `test_variables.py` driving the TFE spellings,
and re-driving each one through a second prefix would duplicate that while
testing nothing new. What is genuinely new is the wiring: that the native path
exists, reaches the *same* function, and carries the same authorization. A
second door that was a weaker one would be a privilege escalation dressed up as
a re-mount, and nothing else in the suite would notice.
"""

from __future__ import annotations

from terrapod.api.app import app

#: (native path, TFE path, methods) for each route that gained a native home.
#:
#: Six of the seven differ only by prefix. The collection is the exception, and
#: the reason it could not simply be re-mounted: it carries an
#: `organizations/default/` segment on the TFE surface, and the native surface
#: must never have one (architecture principle 9, enforced globally by
#: `test_no_native_route_anywhere_carries_an_org_segment`). So the native
#: spelling drops the segment and the two paths differ in shape.
VARSET_ROUTES = [
    ("/api/v1/varsets", "/api/tfe/v2/organizations/default/varsets", {"GET", "POST"}),
    ("/api/v1/varsets/{varset_id}", "/api/tfe/v2/varsets/{varset_id}", {"GET", "PATCH", "DELETE"}),
    (
        "/api/v1/varsets/{varset_id}/relationships/workspaces",
        "/api/tfe/v2/varsets/{varset_id}/relationships/workspaces",
        {"POST", "DELETE"},
    ),
]


def _by_path() -> dict[str, dict[str, object]]:
    """method -> endpoint function, per path, from the live app."""
    out: dict[str, dict[str, object]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None)
        if path is None or endpoint is None:
            continue
        for method in getattr(route, "methods", set()) or set():
            out.setdefault(path, {})[method] = endpoint
    return out


def test_every_varset_route_has_a_native_path() -> None:
    routes = _by_path()
    for native, tfe, methods in VARSET_ROUTES:
        for method in methods:
            assert routes.get(native, {}).get(method) is not None, (
                f"{method} {native} is not served. Variable sets are managed by the "
                f"provider, and reading them on the compatibility surface returns a "
                f"document with the native-only attributes stripped (#2469)."
            )
            assert routes.get(tfe, {}).get(method) is not None, (
                f"{method} {tfe} disappeared. The TFE spelling is frozen: `tfci` drives it."
            )


def test_both_doors_reach_the_same_handler() -> None:
    """Not a copy -- the same function object.

    Two implementations of one route is how the surfaces come to disagree about
    what a variable set is, which is the failure this whole change is undoing.
    """
    routes = _by_path()
    for native, tfe, methods in VARSET_ROUTES:
        for method in methods:
            assert routes[native][method] is routes[tfe][method], (
                f"{method} {native} and {method} {tfe} are different functions; "
                f"they must be one handler mounted twice."
            )


def test_the_native_collection_carries_no_org_segment() -> None:
    """The one route that is a second path rather than a re-mount."""
    routes = _by_path()
    assert "/api/v1/organizations/default/varsets" not in routes, (
        "the org-segmented spelling leaked onto the native surface; principle 9 "
        "forbids it, and a mount is exactly how it arrives as a side effect"
    )
    assert {"GET", "POST"} <= set(routes["/api/v1/varsets"]), (
        "the native collection must serve both list and create"
    )


def test_the_native_writes_keep_their_authorization() -> None:
    """A second door must not be a weaker one.

    Compared against the TFE spelling rather than against a list of expected
    dependencies, so this keeps meaning the right thing if the gate on a route
    is ever tightened or relaxed -- what it refuses is the two surfaces
    DIFFERING, which is the only way a re-mount can widen access.
    """
    routes = _by_path()
    for native, tfe, methods in VARSET_ROUTES:
        for method in methods:
            # The same function object implies the same dependencies, but assert
            # the decorated dependency list too: a future native mount that
            # wrapped the handler would keep the behaviour and lose the gate.
            n = _dependency_names(routes[native][method])
            t = _dependency_names(routes[tfe][method])
            assert n == t, f"{method} {native} guards {n}, {tfe} guards {t}"


def _dependency_names(endpoint: object) -> set[str]:
    """The names of the FastAPI dependencies declared on an endpoint."""
    import inspect

    names: set[str] = set()
    for param in inspect.signature(endpoint).parameters.values():  # type: ignore[arg-type]
        default = param.default
        dep = getattr(default, "dependency", None)
        if dep is not None:
            names.add(getattr(dep, "__name__", repr(dep)))
    return names


def test_the_route_list_is_not_empty() -> None:
    """A floor, so a walk that stops matching fails rather than passing on nothing."""
    assert len(VARSET_ROUTES) >= 3
    assert sum(len(m) for _, _, m in VARSET_ROUTES) == 7, (
        "seven routes gained a native home; update this count deliberately"
    )
