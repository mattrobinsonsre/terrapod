"""The exclude-entry refusal, driven through every route that translates it.

`_exclude_entry_problem` became the one producer of this refusal in #2653,
because three sites had decided it separately. Its own tests call it directly --
forty times -- which pins what it decides and says nothing about whether a
caller still reaches it, or still turns its answer into a 422 rather than a 500.
That is the gap this closes, and #2648 is why it matters: one of the three sites
was answering 500 for an invalid filter until it was found.

Two routes translate it for a caller (`search_workspaces`, `bulk_update_workspaces`)
and `_report_exclusions` raises in its own right. Driven as requests, so deleting
a handler fails a test here rather than passing on the direct-call tests.

TWO producers consult the shared rule on the wire path, in sequence, and that is
deliberate rather than redundant: `_parse_entry` checks the dict as it parses it
and `_check_exclude_entry` checks the model it produced. So silencing either one
alone leaves these tests green -- measured, not assumed -- and silencing both
fails five of the six. A mutation aimed at one producer therefore proves nothing
here; aim at both, or at the rule they share.

The relaxation is asserted too: `all: false` on an exclude entry is ACCEPTED.
Refusing it refused a statement that means nothing -- "not all" is already the
default -- and a test that only pins refusals teaches the next person that
stricter is always safer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.db.session import get_db
from terrapod.services.workspace_search_service import ExclusionReport

# `_mock_ws` and the report patch target are REUSED rather than rebuilt. A
# hand-rolled workspace stand-in here missed `engine_version` on its first run,
# and that is the documented failure mode of this class of fixture: every file
# carrying its own means a new serialized attribute breaks each of them
# separately, with a message naming neither the attribute nor the file.
from tests.api.test_workspace_bulk import _REPORT, _list_result
from tests.api.test_workspace_bulk import _mock_ws as _ws

_BASE = "http://testserver"
_AUTH = {"Authorization": "Bearer t"}

#: Both prefixes, because a route that decides something must behave the same on
#: the canonical path and the deprecated alias -- and the alias is what every
#: other test in this area still uses, so a new test on one prefix alone would
#: not notice the other breaking.
SEARCH = ("/api/v1/workspaces/actions/search", "/api/terrapod/v1/workspaces/actions/search")
BULK = (
    "/api/v1/workspaces/actions/bulk-update",
    "/api/terrapod/v1/workspaces/actions/bulk-update",
)


def _admin() -> AuthenticatedUser:
    # Copied from test_workspace_bulk.py rather than reconstructed: this class
    # takes no `capabilities`, which a first attempt here invented.
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user: AuthenticatedUser) -> tuple[object, AsyncMock]:
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: user
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


#: Each is an exclude entry that `_exclude_entry_problem` refuses, with the
#: fragment of its message that says WHY -- asserting the status alone cannot
#: tell one refusal from another, and three different reasons share the 422.
REFUSED = (
    ({"all": True}, "cannot set"),
    ({"exclude": [{"labels": {"x": "y"}}]}, "cannot set"),
    ({}, "sets no selector"),
)


class TestTheSearchRouteRefuses:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_every_invalid_exclude_entry_is_a_422_that_says_why(self, *_m) -> None:
        app, _db = _make_app(_admin())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            for entry, fragment in REFUSED:
                body = {"filter": {"all": True, "exclude": [entry]}}
                resp = await c.post(SEARCH[0], json=body, headers=_AUTH)
                assert resp.status_code == 422, f"{entry} -> {resp.status_code}"
                detail = resp.json()["detail"]
                assert "exclude[0]" in detail, f"{entry}: the message must name the entry: {detail}"
                assert fragment in detail, f"{entry}: wrong refusal: {detail}"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_the_index_in_the_message_is_the_offending_entry(self, *_m) -> None:
        # A second valid entry ahead of it, so an off-by-one in the label is
        # visible rather than hidden by the only index being 0.
        app, _db = _make_app(_admin())
        body = {"filter": {"all": True, "exclude": [{"labels": {"a": "b"}}, {"all": True}]}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(SEARCH[0], json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "exclude[1]" in resp.json()["detail"]

    async def test_all_false_on_an_exclude_entry_is_accepted(self) -> None:
        # The #2653 relaxation. `all: false` means "not all", which is the
        # default, so there was nothing to refuse.
        #
        # `_report_exclusions` is patched for the same reason the sibling file
        # patches it: the real one runs against the mocked session and returns
        # MagicMocks, which reach `json.dumps` and fail for a reason that has
        # nothing to do with whether the entry was accepted.
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result([_ws(name="w1")]))
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        body = {"filter": {"all": True, "exclude": [{"all": False, "labels": {"a": "b"}}]}}
        with (
            patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
            patch("terrapod.api.app.init_redis"),
            patch("terrapod.api.app.init_db"),
            patch(
                _REPORT, new_callable=AsyncMock, return_value=ExclusionReport(excluded=0, empty=[])
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(SEARCH[0], json=body, headers=_AUTH)
        assert resp.status_code == 200, resp.text


class TestTheBulkRouteRefusesIdentically:
    """The second translation site, and the one whose filter also writes."""

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_an_invalid_exclude_entry_is_a_422_before_anything_is_written(self, *_m) -> None:
        app, db = _make_app(_admin())
        body = {
            "filter": {"all": True, "exclude": [{"all": True}]},
            "set": {"execution-mode": "local"},
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(BULK[0], json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "exclude[0]" in resp.json()["detail"]
        # The refusal is a validation failure, so nothing may have been committed
        # -- the file's own zero-mutation contract.
        assert db.commit.await_count == 0

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_both_routes_give_the_same_message_for_the_same_entry(self, *_m) -> None:
        # The point of one producer: two routes cannot drift into describing the
        # same invalid filter differently.
        app, _db = _make_app(_admin())
        entry = {"all": True}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            a = await c.post(
                SEARCH[0], json={"filter": {"all": True, "exclude": [entry]}}, headers=_AUTH
            )
            b = await c.post(
                BULK[0],
                json={
                    "filter": {"all": True, "exclude": [entry]},
                    "set": {"execution-mode": "local"},
                },
                headers=_AUTH,
            )
        assert a.status_code == b.status_code == 422
        assert a.json()["detail"] == b.json()["detail"]


class TestTheAliasBehavesTheSame:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_the_refusal_is_the_same_on_the_deprecated_alias(self, *_m) -> None:
        app, _db = _make_app(_admin())
        body = {"filter": {"all": True, "exclude": [{"all": True}]}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            canonical = await c.post(SEARCH[0], json=body, headers=_AUTH)
            alias = await c.post(SEARCH[1], json=body, headers=_AUTH)
        assert canonical.status_code == alias.status_code == 422
        assert canonical.json()["detail"] == alias.json()["detail"]
