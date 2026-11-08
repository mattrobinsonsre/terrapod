"""`reachable()` tells "not published" apart from "could not tell" (#2425).

It used to return a bool, so a 404 and a timeout were the same answer and the
failure message said *not published* either way. During a GitHub release-CDN
incident that failed every PR with a message sending the reader to fix a version
pin that was perfectly fine — which is what the function's own docstring warns
against, through a door it had not anticipated. It happened to `trivy 0.76.2`,
which was reported unpublished while being published with both claimed assets
present.

So the properties worth pinning are the DISTINCTION and the retry, not the happy
path: an HTTP status is evidence about the artifact, a transport error is
evidence about the network, and a transient one must recover rather than decide.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import _load

gate = _load("check-platform-tool-artifacts")


class _FakeClient:
    """Answers `head`/`get` from scripted queues, counting every call.

    Scripted rather than a stub returning one value: the retry is a property of
    the SEQUENCE, so a client that cannot change its answer cannot express the
    case that matters — a failure followed by a success.
    """

    def __init__(self, heads: list[object], gets: list[object] | None = None) -> None:
        self._heads = list(heads)
        self._gets = list(gets or [])
        self.head_calls = 0
        self.get_calls = 0

    @staticmethod
    def _answer(queue: list[object], which: str) -> httpx.Response:
        if not queue:
            raise AssertionError(f"{which} called more times than the test scripted")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(int(item))

    def head(self, _url: str, **_kw: object) -> httpx.Response:
        self.head_calls += 1
        return self._answer(self._heads, "head")

    def get(self, _url: str, **_kw: object) -> httpx.Response:
        self.get_calls += 1
        return self._answer(self._gets, "get")


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backoff is real seconds; the test asserts the retry, not the wait."""
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)


def test_a_published_asset_is_ok() -> None:
    client = _FakeClient(heads=[200])
    outcome, why = gate.reachable(client, "https://example.test/a.tar.gz")
    assert outcome == "ok"
    assert "HEAD 200" in why


def test_a_head_the_store_refuses_is_confirmed_by_a_ranged_get() -> None:
    """Some object stores answer 403 to a HEAD they would serve as a GET."""
    client = _FakeClient(heads=[403], gets=[206])
    outcome, _ = gate.reachable(client, "https://example.test/a.tar.gz")
    assert outcome == "ok"
    assert client.get_calls == 1


def test_a_404_is_ABSENT_and_is_not_retried() -> None:
    """A status is evidence about the artifact, and a 404 does not improve.

    Retrying it would turn every genuinely-wrong version pin into three times
    the wait before the same answer.
    """
    client = _FakeClient(heads=[404], gets=[404])
    outcome, why = gate.reachable(client, "https://example.test/a.tar.gz")
    assert outcome == "absent"
    assert "404" in why
    assert client.head_calls == 1, "a 404 was retried"


def test_a_transport_error_is_UNVERIFIED_rather_than_absent() -> None:
    """The distinction this function exists for.

    `unverified` still fails the build — an unverifiable claim is not a verified
    one — but it must not tell the operator their version is unpublished.
    """
    boom = httpx.ReadTimeout("the read operation timed out")
    client = _FakeClient(heads=[boom] * gate.TRANSPORT_ATTEMPTS)
    outcome, why = gate.reachable(client, "https://example.test/a.tar.gz")
    assert outcome == "unverified"
    assert outcome != "absent"
    assert "ReadTimeout" in why
    assert f"{gate.TRANSPORT_ATTEMPTS} attempts" in why
    assert client.head_calls == gate.TRANSPORT_ATTEMPTS


def test_a_TRANSIENT_transport_error_recovers() -> None:
    """The point of the retry, and the case the incident was.

    Without this the test above would pass against an implementation that simply
    relabelled the failure, which would leave every PR red for the duration of
    somebody else's outage.
    """
    client = _FakeClient(
        heads=[httpx.RemoteProtocolError("server disconnected"), 200],
    )
    outcome, _ = gate.reachable(client, "https://example.test/a.tar.gz")
    assert outcome == "ok"
    assert client.head_calls == 2


def test_the_three_outcomes_are_the_only_ones() -> None:
    """`main` maps the outcome to a label by dict lookup, so a fourth value
    would raise a KeyError at the moment it is printed rather than be reported.
    Asserting the closed set here is what keeps that lookup total."""
    seen = set()
    for heads, gets in (
        ([200], []),
        ([404], [404]),
        ([httpx.ConnectError("no route")] * gate.TRANSPORT_ATTEMPTS, []),
    ):
        seen.add(gate.reachable(_FakeClient(heads, gets), "https://example.test/a")[0])
    assert seen == {"ok", "absent", "unverified"}
