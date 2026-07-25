"""Router tests for the AI architecture-critique endpoints (#963/#1036).

  - GET  /api/terrapod/v1/runs/{run_id}/architecture-critique
  - POST /api/terrapod/v1/runs/{run_id}/architecture-critique/regenerate
  - GET/POST .../architecture-critique/messages

Near-exact clone of test_cost_summary.py's harness (create_app + dependency
overrides + mocked DB/RBAC).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth.capabilities import caps_for_level
from terrapod.db.session import get_db

_BASE = "http://testserver"
_AUTH = {"Authorization": "Bearer dummy"}


def _user():
    return AuthenticatedUser(
        email="test@example.com",
        display_name="Test",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _mock_run(has_json_output=True):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.status = "planned"
    run.has_json_output = has_json_output
    return run


def _mock_ws(ws_id):
    ws = MagicMock()
    ws.id = ws_id
    ws.owner_email = "test@example.com"
    ws.labels = {}
    return ws


def _mock_critique(run_id, status="ready"):
    c = MagicMock()
    c.id = uuid.uuid4()
    c.run_id = run_id
    c.status = status
    c.critique = "The proposed VPC is single-AZ." if status == "ready" else ""
    c.findings = (
        [
            {
                "severity": "high",
                "category": "reliability",
                "title": "Single-AZ database",
                "detail": "aws_db_instance.main has no multi_az.",
                "address": "aws_db_instance.main",
            }
        ]
        if status == "ready"
        else []
    )
    c.risk_level = "high" if status == "ready" else ""
    c.model = "test-model"
    c.input_tokens = 100
    c.output_tokens = 50
    c.error_message = ""
    c.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    c.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return c


def _make_app(user, mock_db):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db
    return app


class TestGetArchitectureCritique:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_returns_critique(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        critique = _mock_critique(run.id)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=critique)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        app = _make_app(_user(), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique", headers=_AUTH
            )

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["status"] == "ready"
        assert attrs["risk-level"] == "high"
        assert attrs["critique"].startswith("The proposed")
        assert attrs["findings"][0]["category"] == "reliability"
        assert attrs["findings"][0]["address"] == "aws_db_instance.main"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_translated_on_locale(self, mock_resolve, *_):
        # ?locale= translates the prose on view; data fields stay verbatim, and
        # translated/language reflect the translation.
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        critique = _mock_critique(run.id)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=critique)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        translated = {
            "critique": "Kritik DE",
            "findings": [
                {
                    "severity": "high",
                    "category": "reliability",
                    "title": "Titel DE",
                    "detail": "Detail DE",
                    "address": "aws_db_instance.main",
                }
            ],
        }

        app = _make_app(_user(), mock_db)
        with (
            patch(
                "terrapod.services.summary_translation.translate_architecture_critique",
                AsyncMock(return_value=translated),
            ),
            patch("terrapod.api.routers.runs.settings.ai_summary.summary_language", "en"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(
                    f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique?locale=de",
                    headers=_AUTH,
                )

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["translated"] is True
        assert attrs["language"] == "en"
        assert attrs["critique"] == "Kritik DE"
        assert attrs["findings"][0]["title"] == "Titel DE"
        # Non-prose fields preserved through translation.
        assert attrs["findings"][0]["severity"] == "high"
        assert attrs["findings"][0]["address"] == "aws_db_instance.main"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_404_when_absent(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        app = _make_app(_user(), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique", headers=_AUTH
            )
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_403_when_no_read(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level(None)  # no capabilities
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        ws.owner_email = "someone-else@example.com"
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[MagicMock(scalar_one_or_none=MagicMock(return_value=run))]
        )
        mock_db.get = AsyncMock(return_value=ws)

        app = _make_app(_user(), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique", headers=_AUTH
            )
        assert resp.status_code == 403


class TestRegenerateArchitectureCritique:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_202_enqueues(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run(has_json_output=True)
        ws = _mock_ws(run.workspace_id)
        pending = _mock_critique(run.id, status="pending")
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),  # _get_run
                MagicMock(),  # pg_insert
                MagicMock(scalar_one_or_none=MagicMock(return_value=pending)),  # re-read
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)
        mock_db.commit = AsyncMock()

        app = _make_app(_user(), mock_db)
        with (
            patch("terrapod.api.routers.runs.settings.ai_summary.enabled", True),
            patch("terrapod.services.scheduler.enqueue_trigger", AsyncMock()) as enq,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique/regenerate",
                    headers=_AUTH,
                )
        assert resp.status_code == 202
        assert resp.json()["data"]["attributes"]["status"] == "pending"
        enq.assert_awaited_once()  # fired the trigger (dedup bypassed)

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_503_when_disabled(self, *_):
        run = _mock_run()
        mock_db = AsyncMock()
        app = _make_app(_user(), mock_db)
        with patch("terrapod.api.routers.runs.settings.ai_summary.enabled", False):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique/regenerate",
                    headers=_AUTH,
                )
        assert resp.status_code == 503

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_409_when_no_plan_json(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run(has_json_output=False)
        ws = _mock_ws(run.workspace_id)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[MagicMock(scalar_one_or_none=MagicMock(return_value=run))]
        )
        mock_db.get = AsyncMock(return_value=ws)

        app = _make_app(_user(), mock_db)
        with patch("terrapod.api.routers.runs.settings.ai_summary.enabled", True):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique/regenerate",
                    headers=_AUTH,
                )
        assert resp.status_code == 409


def _mock_message(content="reply", role="assistant"):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.role = role
    m.content = content
    m.model = "test-model"
    m.input_tokens = 100
    m.output_tokens = 25
    m.error_message = ""
    m.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return m


class TestArchitectureCritiqueChat:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs._resolve_architecture_critique_for_chat")
    async def test_post_reply(self, mock_resolve, *_):
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        critique = _mock_critique(run.id)
        mock_resolve.return_value = (run, critique, ws)
        assistant = _mock_message(content="Add a read replica.")

        app = _make_app(_user(), AsyncMock())
        with patch(
            "terrapod.services.architecture_critic.post_architecture_followup",
            new_callable=AsyncMock,
            return_value=assistant,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique/messages",
                    headers=_AUTH,
                    json={"data": {"attributes": {"content": "how do I make it HA?"}}},
                )
        assert resp.status_code == 201
        attrs = resp.json()["data"]["attributes"]
        assert attrs["role"] == "assistant"
        assert attrs["content"].startswith("Add a read")
        assert attrs["translated"] is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs._resolve_architecture_critique_for_chat")
    async def test_post_cap_reached_is_409(self, mock_resolve, *_):
        from terrapod.services.summariser import FollowupCapReached

        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        critique = _mock_critique(run.id)
        mock_resolve.return_value = (run, critique, ws)

        app = _make_app(_user(), AsyncMock())
        with patch(
            "terrapod.services.architecture_critic.post_architecture_followup",
            new_callable=AsyncMock,
            side_effect=FollowupCapReached("cap hit"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique/messages",
                    headers=_AUTH,
                    json={"data": {"attributes": {"content": "hi"}}},
                )
        assert resp.status_code == 409

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs._resolve_architecture_critique_for_chat")
    async def test_post_disabled_is_503(self, mock_resolve, *_):
        from terrapod.services.summariser import FollowupDisabled

        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        critique = _mock_critique(run.id)
        mock_resolve.return_value = (run, critique, ws)

        app = _make_app(_user(), AsyncMock())
        with patch(
            "terrapod.services.architecture_critic.post_architecture_followup",
            new_callable=AsyncMock,
            side_effect=FollowupDisabled("off"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/architecture-critique/messages",
                    headers=_AUTH,
                    json={"data": {"attributes": {"content": "hi"}}},
                )
        assert resp.status_code == 503
