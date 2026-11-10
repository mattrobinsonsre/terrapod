"""The routes that write and serve `default-run-kind` (#2479).

Driven through the ROUTE in every case, never by calling the validator. That is
the distinction this file exists for: `validate_default_run_kind` has its own
unit tests in tests/services/test_default_run_kind.py and every one of them
passes with the call site deleted -- which is exactly how a 422 guard ships as a
500. The mutations to run against this file are removing the
`workspace_settings.validate_default_run_kind` call from `create_workspace`,
from the PATCH handler, or from the autodiscovery rule builder, and removing
`_reject_a_run_kind_a_workspace_cannot_offer` from the bulk update.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.auth.capabilities import caps_for_level
from tests.api.test_workspaces import (
    _AUTH,
    _BASE,
    _make_app,
    _mock_workspace,
    _no_inert_vars,
    _user,
)

INIT = (
    patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
    patch("terrapod.api.app.init_redis"),
    patch("terrapod.api.app.init_db"),
    patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock),
)


def _app_for_create():
    app, db = _make_app(_user(roles=["admin"]))
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    db.refresh = AsyncMock()
    added: list = []
    db.add = added.append
    return app, added


def _app_for_patch(ws):
    app, db = _make_app(_user(roles=["admin"]))
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    db.execute.return_value = result
    db.refresh = AsyncMock()
    return app


async def _post(app, attrs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.post(
            "/api/v1/workspaces", json={"data": {"attributes": attrs}}, headers=_AUTH
        )


async def _patch(app, ws, attrs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.patch(
            f"/api/v1/workspaces/ws-{ws.id}",
            json={"data": {"attributes": attrs}},
            headers=_AUTH,
        )


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock)
class TestCreate:
    async def test_a_kind_the_new_workspace_would_not_offer_is_refused(self, _pub, *_m):
        """The operation flags in the SAME payload decide it, not the defaults.

        `configure-enabled` defaults false, so a create naming `configure` and
        saying nothing else is asking for a preference the new workspace could
        never honour.
        """
        app, added = _app_for_create()
        resp = await _post(app, {"name": "ws-a", "default-run-kind": "configure"})
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "configure-enabled" in detail, (
            f"the refusal must name the flag the caller has to set: {detail}"
        )
        assert not added, "a refused create still added a row"

    async def test_the_same_kind_is_accepted_when_the_payload_grants_the_operation(self, _pub, *_m):
        app, added = _app_for_create()
        resp = await _post(
            app,
            {
                "name": "ws-b",
                "configure-enabled": True,
                "default-run-kind": "configure",
            },
        )
        assert resp.status_code == 201, resp.text
        ws = next(w for w in added if getattr(w, "name", None) == "ws-b")
        assert ws.default_run_kind == "configure"

    async def test_an_omitted_attribute_stores_no_preference(self, _pub, *_m):
        """Not a MagicMock and not None: the column is NOT NULL and `""` is the
        value meaning "no preference"."""
        app, added = _app_for_create()
        resp = await _post(app, {"name": "ws-c"})
        assert resp.status_code == 201, resp.text
        ws = next(w for w in added if getattr(w, "name", None) == "ws-c")
        assert ws.default_run_kind == ""

    async def test_full_needs_both_operations(self, _pub, *_m):
        app, added = _app_for_create()
        resp = await _post(app, {"name": "ws-d", "default-run-kind": "full"})
        assert resp.status_code == 422, resp.text
        assert "configure-enabled" in resp.json()["detail"]
        assert not added

    async def test_a_word_outside_the_vocabulary_is_a_422_not_a_500(self, _pub, *_m):
        app, added = _app_for_create()
        resp = await _post(app, {"name": "ws-e", "default-run-kind": "deploy"})
        assert resp.status_code == 422, resp.text
        assert "must be empty or one of" in resp.json()["detail"]
        assert not added

    async def test_a_non_string_is_a_422_not_a_500(self, _pub, *_m):
        """The sibling attributes answer 422 for a wrong type, so this one has
        to as well -- a 500 for a bad request body is the shape the `_422`
        wrapper exists to prevent."""
        app, added = _app_for_create()
        resp = await _post(app, {"name": "ws-f", "default-run-kind": 7})
        assert resp.status_code == 422, resp.text
        assert "must be a string" in resp.json()["detail"]
        assert not added


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock)
@patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
class TestPatch:
    async def test_setting_only_the_kind_is_judged_against_the_stored_row(
        self, mock_resolve, _pub, *_m
    ):
        """The request body carries no operation flags at all, so the check has
        to read the workspace."""
        mock_resolve.return_value = caps_for_level("admin")

        ws = _mock_workspace(name="has-configure")
        ws.plan_apply_enabled = True
        ws.configure_enabled = True
        resp = await _patch(_app_for_patch(ws), ws, {"default-run-kind": "configure"})
        assert resp.status_code == 200, resp.text
        assert ws.default_run_kind == "configure"

        ws2 = _mock_workspace(name="plan-only")
        ws2.plan_apply_enabled = True
        ws2.configure_enabled = False
        resp = await _patch(_app_for_patch(ws2), ws2, {"default-run-kind": "configure"})
        assert resp.status_code == 422, resp.text
        assert "configure-enabled" in resp.json()["detail"]
        assert ws2.default_run_kind == "", "a refused PATCH still wrote the column"

    async def test_turning_the_operation_off_is_refused_while_the_kind_names_it(
        self, mock_resolve, _pub, *_m
    ):
        """SUPERSEDED, and which half of its reason still holds.

        This asserted a 422. The reason stands -- a payload mentioning only
        `configure-enabled` must not leave the workspace holding a preference
        nothing can honour, stored and ignored with nothing to tell the operator
        -- and the ANSWER changed, because the refusal was unanswerable by the
        caller most likely to trip it: a client two minors behind has no
        `default-run-kind` field, so "clear default-run-kind" named something it
        could not send and every later apply stayed wedged. That is OLD client
        against NEW server, the direction `api >= sdk` supports.

        The orphaned preference is DROPPED instead
        (`workspace_settings.reconcile_default_run_kind`). The invariant is
        unchanged -- the workspace does not end up naming a kind it cannot offer
        -- and the cost is a button's position, because this setting is ordering
        only. A caller that NAMES a conflicting kind is still refused; that is
        `test_a_named_contradiction_is_still_refused` below.
        """
        mock_resolve.return_value = caps_for_level("admin")

        ws = _mock_workspace(name="prefers-configure")
        ws.plan_apply_enabled = True
        ws.configure_enabled = True
        ws.default_run_kind = "configure"

        resp = await _patch(_app_for_patch(ws), ws, {"configure-enabled": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["default-run-kind"] == "", (
            "the orphaned preference survived a write that removed the operation it names"
        )
        assert ws.configure_enabled is False

    async def test_a_named_contradiction_is_still_refused(self, mock_resolve, _pub, *_m):
        """The other half, so the change above cannot read as "the refusal went".

        Both fields in one payload: the caller can see the contradiction and fix
        it, so it is refused, and the message still names the preference rather
        than only the flag.
        """
        mock_resolve.return_value = caps_for_level("admin")

        ws = _mock_workspace(name="names-it")
        ws.plan_apply_enabled = True
        ws.configure_enabled = True
        ws.default_run_kind = ""

        resp = await _patch(
            _app_for_patch(ws),
            ws,
            {"configure-enabled": False, "default-run-kind": "configure"},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "configure-enabled" in detail
        assert "configure" in detail, (
            "the refusal must name the preference that blocks the change, not "
            f"only the flag being changed: {detail}"
        )

    async def test_clearing_the_kind_in_the_same_call_lets_the_operation_go_off(
        self, mock_resolve, _pub, *_m
    ):
        """And the remedy the refusal above tells the operator to use has to
        work, or the refusal is a dead end."""
        mock_resolve.return_value = caps_for_level("admin")

        ws = _mock_workspace(name="prefers-configure")
        ws.plan_apply_enabled = True
        ws.configure_enabled = True
        ws.default_run_kind = "configure"

        resp = await _patch(
            _app_for_patch(ws),
            ws,
            {"configure-enabled": False, "default-run-kind": ""},
        )
        assert resp.status_code == 200, resp.text
        assert ws.configure_enabled is False
        assert ws.default_run_kind == ""

    async def test_an_unmentioned_attribute_keeps_its_stored_value(self, mock_resolve, _pub, *_m):
        """A partial update stays partial: a PATCH about something else must not
        clear the preference."""
        mock_resolve.return_value = caps_for_level("admin")

        ws = _mock_workspace(name="prefers-full")
        ws.plan_apply_enabled = True
        ws.configure_enabled = True
        ws.default_run_kind = "full"

        resp = await _patch(_app_for_patch(ws), ws, {"auto-configure-enabled": True})
        assert resp.status_code == 200, resp.text
        assert ws.default_run_kind == "full"


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
@patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock)
@patch("terrapod.api.routers.tfe_v2.resolve_workspace_capabilities_for")
class TestTheNativeWire:
    """The native surface serves it.

    The other half -- that the TFE-compatible surface STRIPS it -- is asserted
    beside its siblings in test_workspaces.py rather than here, because that
    list is the one producer of "what is native-only" and a second copy is how
    the two come to disagree.
    """

    @pytest.mark.parametrize("stored", ["", "full", "configure"])
    async def test_it_is_served_as_stored(self, mock_resolve, _pub, _db, _redis, _storage, stored):
        """Including the empty string, AS an empty string rather than as an
        absent key or a null: `""` is a value, and a client that writes a read
        straight back must be handed what it stored."""
        mock_resolve.return_value = caps_for_level("admin")
        ws = _mock_workspace(name="served-ws")
        ws.default_run_kind = stored

        app, db = _make_app(_user(roles=["admin"]))
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run = MagicMock()
        no_run.scalar_one_or_none.return_value = None
        db.execute.side_effect = [ws_result, no_run, _no_inert_vars()]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v1/workspaces/ws-{ws.id}", headers=_AUTH)

        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert "default-run-kind" in attrs, (
            "the native surface does not serve default-run-kind, so a provider "
            "cannot read back what it wrote"
        )
        assert attrs["default-run-kind"] == stored
