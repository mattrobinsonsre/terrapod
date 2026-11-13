"""A bad cron or zone is refused at every write path that accepts it (#2538).

`tests/services/test_scheduled_configure_service.py` exercises the two rules
thoroughly. Nothing there pins that either is *called* at a router, which is the
gap `test_oidc_audiences_write_paths` was written for and the same one here: a
validator reached only from its own unit test leaves the route answering **500**
on a typo instead of a 422 naming the field.

The invalid cron used throughout is `*/1 * * * *` rather than a type error,
because it is the one the floor exists for -- a configure can run for an hour
and is not safely restartable, so a schedule firing every minute is the case
with a consequence beyond an ugly row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db
from tests.api.test_workspaces import _mock_workspace

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_CREATE = "/api/v2/organizations/default/workspaces"

#: Fires every 60s, which the floor refuses.
TOO_OFTEN = "*/1 * * * *"
#: The same rate spelled so the first two occurrences look an hour apart.
TOO_OFTEN_DISGUISED = "0,1 * * * *"
#: Six fields: seconds resolution, which cannot be honoured.
SECONDS = "*/5 * * * * *"
VALID_CRON = "0 2 * * 2"
VALID_ZONE = "Europe/London"


def _user(roles=None):
    return AuthenticatedUser(
        email="test@example.com",
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _app(user):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    return app, db


def _no_existing(db):
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)


def _existing(db, ws):
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    db.execute = AsyncMock(return_value=result)


def _created(db):
    return next(
        c.args[0]
        for c in db.add.call_args_list
        if getattr(c.args[0], "__tablename__", "") == "workspaces"
    )


@pytest.fixture(autouse=True)
def _no_event_publish():
    with patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock):
        yield


class TestWorkspaceCreate:
    """`POST /api/v2/organizations/default/workspaces`."""

    async def _post(self, attributes):
        app, db = _app(_user(roles=["admin"]))
        _no_existing(db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                _CREATE,
                json={"data": {"type": "workspaces", "attributes": attributes}},
                headers=_AUTH,
            )
        return resp, db

    @pytest.mark.parametrize("bad", [TOO_OFTEN, TOO_OFTEN_DISGUISED, SECONDS, "nonsense", 5])
    async def test_an_unusable_cron_is_refused_before_the_workspace_exists(self, bad):
        resp, db = await self._post({"name": "sched-bad", "scheduled-configure-cron": bad})
        assert resp.status_code == 422, resp.text
        assert "scheduled-configure-cron" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    @pytest.mark.parametrize("bad", ["Mars/Olympus", "not a zone", 5])
    async def test_an_unusable_timezone_is_refused(self, bad):
        resp, db = await self._post({"name": "sched-tz", "scheduled-configure-timezone": bad})
        assert resp.status_code == 422, resp.text
        assert "scheduled-configure-timezone" in resp.json()["detail"]
        db.commit.assert_not_awaited()

    async def test_a_valid_schedule_is_stored_byte_for_byte(self):
        """The no-normalisation property. The provider writes the server's
        answer back into state, so reformatting the expression here would make
        every plan disagree with its own apply."""
        resp, db = await self._post(
            {
                "name": "sched-ok",
                "scheduled-configure-enabled": True,
                "scheduled-configure-cron": VALID_CRON,
                "scheduled-configure-timezone": VALID_ZONE,
            }
        )
        assert resp.status_code == 201, resp.text
        ws = _created(db)
        assert ws.scheduled_configure_cron == VALID_CRON
        assert ws.scheduled_configure_timezone == VALID_ZONE

    async def test_creating_it_ENABLED_stamps_so_it_does_not_fire_on_sight(self):
        """Without the stamp the next occurrence is computed from `created_at`
        -- already in the past -- and the workspace configures its fleet the
        moment it exists."""
        resp, db = await self._post(
            {
                "name": "sched-stamp",
                "scheduled-configure-enabled": True,
                "scheduled-configure-cron": VALID_CRON,
            }
        )
        assert resp.status_code == 201, resp.text
        assert _created(db).scheduled_configure_last_at is not None

    async def test_creating_it_DISABLED_does_not_stamp(self):
        resp, db = await self._post({"name": "sched-off"})
        assert resp.status_code == 201, resp.text
        assert _created(db).scheduled_configure_last_at is None


class TestWorkspaceUpdate:
    """`PATCH /api/v2/workspaces/{id}`."""

    async def _patch(self, attributes, ws=None):
        ws = ws or _mock_workspace()
        app, db = _app(_user())
        _existing(db, ws)
        with patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for") as caps:
            caps.return_value = caps_for_level("admin")
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.patch(
                    f"/api/v2/workspaces/ws-{ws.id}",
                    json={"data": {"attributes": attributes}},
                    headers=_AUTH,
                )
        return resp, ws

    @pytest.mark.parametrize("bad", [TOO_OFTEN, TOO_OFTEN_DISGUISED, SECONDS, "nonsense"])
    async def test_an_unusable_cron_is_a_422_not_a_500(self, bad):
        resp, _ = await self._patch({"scheduled-configure-cron": bad})
        assert resp.status_code == 422, resp.text
        assert "scheduled-configure-cron" in resp.json()["detail"]

    async def test_an_unusable_timezone_is_a_422_not_a_500(self):
        resp, _ = await self._patch({"scheduled-configure-timezone": "Mars/Olympus"})
        assert resp.status_code == 422, resp.text

    async def test_a_valid_schedule_is_applied(self):
        resp, ws = await self._patch(
            {
                "scheduled-configure-cron": VALID_CRON,
                "scheduled-configure-timezone": VALID_ZONE,
            }
        )
        assert resp.status_code == 200, resp.text
        assert ws.scheduled_configure_cron == VALID_CRON
        assert ws.scheduled_configure_timezone == VALID_ZONE

    async def test_turning_it_ON_stamps_so_it_waits_for_the_next_occurrence(self):
        ws = _mock_workspace()
        ws.scheduled_configure_enabled = False
        ws.scheduled_configure_last_at = None
        resp, ws = await self._patch({"scheduled-configure-enabled": True}, ws=ws)
        assert resp.status_code == 200, resp.text
        assert ws.scheduled_configure_last_at is not None

    async def test_an_already_ON_schedule_is_not_RE_stamped(self):
        """Re-sending `true` must not push the next occurrence out. Stamping on
        every PATCH would let a workspace edited daily never reach 02:00."""
        ws = _mock_workspace()
        ws.scheduled_configure_enabled = True
        ws.scheduled_configure_last_at = None
        resp, ws = await self._patch({"scheduled-configure-enabled": True}, ws=ws)
        assert resp.status_code == 200, resp.text
        assert ws.scheduled_configure_last_at is None

    async def test_an_empty_cron_CLEARS_the_schedule(self):
        """`""` is a real value here -- it is how a schedule is removed -- so it
        must not be read as "leave alone"."""
        ws = _mock_workspace()
        ws.scheduled_configure_cron = VALID_CRON
        resp, ws = await self._patch({"scheduled-configure-cron": ""}, ws=ws)
        assert resp.status_code == 200, resp.text
        assert ws.scheduled_configure_cron == ""

    async def test_an_explicit_empty_timezone_round_trips_rather_than_becoming_UTC(self):
        """Collapsing `""` to `"UTC"` is a TRANSFORM of a value a provider sent:
        `""` planned against `"UTC"` read back is "Provider produced
        inconsistent result after apply"."""
        resp, ws = await self._patch({"scheduled-configure-timezone": ""})
        assert resp.status_code == 200, resp.text
        assert ws.scheduled_configure_timezone == ""
