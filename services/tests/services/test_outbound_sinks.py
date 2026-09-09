"""Every request to a user-supplied URL goes through the guard (#1541).

A source test rather than a behavioural one, deliberately. The property is
"no call site was forgotten", and the way it gets violated is somebody adding a
third sink — which no test of the first two would ever notice. Reading the
source is the only check that fails for the *next* one.

It also replaces a runtime assertion that looked stronger and was weaker: an
`assert client.follow_redirects is False` inside the delivery path is satisfied
by any MagicMock, so it passed in every test while proving nothing about
production, and broke five existing tests by reading a truthy Mock.
"""

from __future__ import annotations

import pathlib

import pytest

SERVICES = pathlib.Path(__file__).resolve().parents[2] / "terrapod/services"

#: Modules that send a request to an address a user chose, and the call each
#: must make first. Adding a sink means adding it here.
USER_ADDRESSED_SINKS = ("notification_service.py", "run_task_dispatcher.py")


@pytest.mark.parametrize("module", USER_ADDRESSED_SINKS)
def test_the_sink_validates_before_requesting(module: str) -> None:
    src = (SERVICES / module).read_text()
    assert "validate_outbound_url" in src, (
        f"{module} sends a request to a user-supplied URL and never calls "
        f"validate_outbound_url — this is the shape of GHSA-q5m2-x8wm-34q9"
    )


@pytest.mark.parametrize("module", USER_ADDRESSED_SINKS)
def test_the_validation_precedes_the_request(module: str) -> None:
    """Order matters: validating after the request has already gone out is not
    a guard, it is a log line."""
    src = (SERVICES / module).read_text()
    first_guard = src.index("await validate_outbound_url")
    first_request = src.index("arequest_with_retry(\n")
    assert first_guard < first_request, f"{module} requests before it validates"


@pytest.mark.parametrize("module", USER_ADDRESSED_SINKS)
def test_redirects_are_not_followed(module: str) -> None:
    """The guard judges one address; a redirect is a request to another that
    nobody judged. httpx defaults this off, so this asserts nobody turned it on
    — enabling it would need per-hop validation first."""
    src = (SERVICES / module).read_text()
    assert "follow_redirects=True" not in src, (
        f"{module} follows redirects, so a permitted URL can bounce to a "
        f"forbidden one without being checked"
    )
