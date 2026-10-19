"""The execution-environment availability route (#2154).

The assertion that matters most is `meta.listing`. The built-in set is read from
a registry, so "this deployment publishes nothing" and "I could not find out"
are different answers with opposite consequences downstream: with `unknown` the
write-time check accepts any line, so a picker must let someone type one rather
than offering an empty select. A route that collapsed the two would make that
impossible to get right in any caller.
"""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.services import ansible_ee_service as ee

_BASE = "http://test"
_URL = "/api/v1/ansible/execution-environments"
_ALIAS = "/api/terrapod/v1/ansible/execution-environments"


def _user(roles=None):
    return AuthenticatedUser(
        email="dev@example.com",
        display_name="Dev",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


def _availability(lines, environments=None):
    return patch.object(
        ee, "availability", AsyncMock(return_value=ee.Availability(lines, environments or {}))
    )


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestTheListing:
    async def test_it_requires_authentication(self, *mocks):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            assert (await c.get(_URL)).status_code == 401

    async def test_any_authenticated_user_may_read_it(self, *mocks):
        """It answers which images this deployment can pull, which every
        workspace editor needs and which an operator's Helm values already say."""
        app = _make_app(_user(roles=["everyone"]))
        with _availability(["2.21"]):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(_URL)
        assert resp.status_code == 200

    async def test_it_is_served_on_the_deprecated_alias_too(self, *mocks):
        """Mounted through `include_terrapod`; a bare mount would serve only the
        canonical path, which is a removal for anything that lags."""
        app = _make_app(_user())
        with _availability(["2.21"]):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                assert (await c.get(_ALIAS)).status_code == 200

    async def test_a_built_in_line_carries_the_image_it_resolves_to(self, *mocks):
        """Resolved by the same single resolver the run path uses, so what is
        shown here cannot drift from what is executed."""
        app = _make_app(_user())
        with (
            patch.object(settings.ansible, "ee_repository", "reg.test/ee"),
            _availability(["2.21", "2.12"]),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                body = (await c.get(_URL)).json()
        assert [d["id"] for d in body["data"]] == ["2.21", "2.12"]
        first = body["data"][0]["attributes"]
        assert first == {
            "kind": "built-in",
            "ansible-core-line": "2.21",
            "label": None,
            "image": "reg.test/ee:2.21",
        }

    async def test_operator_environments_appear_tagged_and_sorted(self, *mocks):
        app = _make_app(_user())
        with _availability([], {"zebra": "r/z:1", "alpha": "r/a:1"}):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                body = (await c.get(_URL)).json()
        assert [d["id"] for d in body["data"]] == ["alpha", "zebra"]
        assert body["data"][0]["attributes"] == {
            "kind": "operator",
            "ansible-core-line": None,
            "label": "alpha",
            "image": "r/a:1",
        }

    async def test_both_kinds_come_back_from_one_route(self, *mocks):
        """Two routes would mean every caller merging two documents, and the
        precedence between them is not symmetric."""
        app = _make_app(_user())
        with _availability(["2.21"], {"hardened": "r/h:1"}):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                body = (await c.get(_URL)).json()
        assert {d["attributes"]["kind"] for d in body["data"]} == {"built-in", "operator"}


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestMetaListing:
    """Empty and unknown must be distinguishable, or no caller can behave correctly."""

    async def test_a_known_set_is_complete(self, *mocks):
        app = _make_app(_user())
        with _availability(["2.21"]):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                body = (await c.get(_URL)).json()
        assert body["meta"]["listing"] == "complete"

    async def test_an_empty_but_known_set_is_still_complete(self, *mocks):
        """The registry answered and holds nothing. A picker should offer nothing."""
        app = _make_app(_user())
        with _availability([]):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                body = (await c.get(_URL)).json()
        assert body["data"] == []
        assert body["meta"]["listing"] == "complete"

    async def test_an_unknown_set_says_so_and_still_lists_operator_entries(self, *mocks):
        """Operator environments come from local configuration, so they are
        known even when the registry is not reachable."""
        app = _make_app(_user())
        with _availability(None, {"hardened": "r/h:1"}):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                body = (await c.get(_URL)).json()
        assert body["meta"]["listing"] == "unknown"
        assert [d["id"] for d in body["data"]] == ["hardened"]

    async def test_the_pagination_meta_is_still_there(self, *mocks):
        """`listing` is added to the shared block, not instead of it."""
        app = _make_app(_user())
        with _availability(["2.21"]):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                meta = (await c.get(_URL)).json()["meta"]
        assert meta["pagination"]["total-count"] == 1
