"""Router tests for the AI cost-narrative endpoints (#871).

  - GET  /api/terrapod/v1/runs/{run_id}/cost-summary
  - POST /api/terrapod/v1/runs/{run_id}/cost-summary/regenerate
  - the `ai-summary-url` gating on GET .../cost-estimate

Mirrors test_ai_summary.py's harness (create_app + dependency overrides +
mocked DB/RBAC).
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


def _mock_run(has_cost_estimate=True):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.workspace_id = uuid.uuid4()
    run.status = "planned"
    run.has_cost_estimate = has_cost_estimate
    return run


def _mock_ws(ws_id):
    ws = MagicMock()
    ws.id = ws_id
    ws.owner_email = "test@example.com"
    ws.labels = {}
    return ws


def _mock_cost_summary(run_id, status="ready"):
    s = MagicMock()
    s.id = uuid.uuid4()
    s.run_id = run_id
    s.status = status
    s.estimated_resources = (
        [
            {
                "address": "azurerm_storage_account.a",
                "type": "azurerm_storage_account",
                "monthly": {"min": 5.0, "max": 8.0},
                "basis": "LRS, hot tier, ~100GB",
                "source": "ai-estimate",
            }
        ]
        if status == "ready"
        else []
    )
    s.narrative = "Roughly $73/mo." if status == "ready" else ""
    s.advisories = (
        [
            {
                "kind": "reserved",
                "title": "t",
                "detail": "d",
                "monthly_saving": None,
                "source": "ai-estimate",
            }
        ]
        if status == "ready"
        else []
    )
    s.model = "test-model"
    s.input_tokens = 100
    s.output_tokens = 50
    s.error_message = ""
    s.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    s.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return s


def _make_app(user, mock_db):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db
    return app


class TestGetCostSummary:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_returns_summary(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        summary = _mock_cost_summary(run.id)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=summary)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        app = _make_app(_user(), mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/terrapod/v1/runs/run-{run.id}/cost-summary", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["status"] == "ready"
        # PRIMARY output surfaced with provenance intact.
        assert attrs["estimated-resources"][0]["address"] == "azurerm_storage_account.a"
        assert attrs["estimated-resources"][0]["source"] == "ai-estimate"
        assert attrs["narrative"].startswith("Roughly")
        assert attrs["advisories"][0]["source"] == "ai-estimate"  # provenance surfaced

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_translated_on_locale(self, mock_resolve, *_):
        # ?locale= translates the prose on view; data fields stay verbatim, and
        # translated/language reflect the translation (#871).
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        summary = _mock_cost_summary(run.id)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=summary)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        translated = {
            "narrative": "Zusammenfassung DE",
            "estimated_resources": [
                {
                    "address": "azurerm_storage_account.a",
                    "type": "azurerm_storage_account",
                    "monthly": {"min": 5.0, "max": 8.0},
                    "basis": "Grundlage DE",
                    "source": "ai-estimate",
                }
            ],
            "advisories": [
                {"kind": "reserved", "title": "Titel DE", "detail": "d", "source": "ai-estimate"}
            ],
        }

        app = _make_app(_user(), mock_db)
        with (
            patch(
                "terrapod.services.summary_translation.translate_cost_summary",
                AsyncMock(return_value=translated),
            ),
            patch("terrapod.api.routers.runs.settings.ai_summary.summary_language", "en"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary?locale=de", headers=_AUTH
                )

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["translated"] is True
        assert attrs["language"] == "en"
        assert attrs["narrative"] == "Zusammenfassung DE"
        assert attrs["estimated-resources"][0]["basis"] == "Grundlage DE"
        # Provenance + numbers preserved through translation.
        assert attrs["estimated-resources"][0]["source"] == "ai-estimate"
        assert attrs["estimated-resources"][0]["monthly"] == {"min": 5.0, "max": 8.0}

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
            resp = await c.get(f"/api/terrapod/v1/runs/run-{run.id}/cost-summary", headers=_AUTH)
        assert resp.status_code == 404


class TestRegenerateCostSummary:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_202_enqueues(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run(has_cost_estimate=True)
        ws = _mock_ws(run.workspace_id)
        pending = _mock_cost_summary(run.id, status="pending")
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
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary/regenerate", headers=_AUTH
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
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary/regenerate", headers=_AUTH
                )
        assert resp.status_code == 503

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_capabilities_for")
    async def test_409_when_no_estimate(self, mock_resolve, *_):
        mock_resolve.return_value = caps_for_level("read")
        run = _mock_run(has_cost_estimate=False)
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
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary/regenerate", headers=_AUTH
                )
        assert resp.status_code == 409


def _mock_cost_message(content="reply", role="assistant"):
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


class TestCostSummaryChat:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs._resolve_cost_summary_for_chat")
    async def test_post_reply(self, mock_resolve, *_):
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        summary = _mock_cost_summary(run.id)
        mock_resolve.return_value = (run, summary, ws)
        assistant = _mock_cost_message(content="An mq.m5.large runs 24/7.")

        app = _make_app(_user(), AsyncMock())
        with patch(
            "terrapod.services.cost_summariser.post_cost_followup",
            new_callable=AsyncMock,
            return_value=assistant,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary/messages",
                    headers=_AUTH,
                    json={"data": {"attributes": {"content": "why so pricey?"}}},
                )
        assert resp.status_code == 201
        attrs = resp.json()["data"]["attributes"]
        assert attrs["role"] == "assistant"
        assert attrs["content"].startswith("An mq")
        assert attrs["translated"] is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs._resolve_cost_summary_for_chat")
    async def test_post_normalizes_and_translates_on_locale(self, mock_resolve, *_):
        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        summary = _mock_cost_summary(run.id)
        mock_resolve.return_value = (run, summary, ws)
        assistant = _mock_cost_message(content="English reply")

        app = _make_app(_user(), AsyncMock())
        with (
            patch(
                "terrapod.services.summary_translation.normalize_to_system_language",
                new_callable=AsyncMock,
                return_value="Warum so teuer?",
            ) as norm,
            patch(
                "terrapod.services.cost_summariser.post_cost_followup",
                new_callable=AsyncMock,
                return_value=assistant,
            ) as pf,
            patch(
                "terrapod.services.summary_translation.translate_message",
                new_callable=AsyncMock,
                return_value="Deutsche Antwort",
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary/messages",
                    headers=_AUTH,
                    json={"data": {"attributes": {"content": "pourquoi?", "locale": "de"}}},
                )
        assert resp.status_code == 201
        attrs = resp.json()["data"]["attributes"]
        assert attrs["content"] == "Deutsche Antwort"
        assert attrs["translated"] is True
        norm.assert_awaited_once_with("pourquoi?", "de")
        # The normalised (system-language) text is what reaches the service.
        assert pf.await_args.kwargs["user_message_text"] == "Warum so teuer?"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs._resolve_cost_summary_for_chat")
    async def test_post_cap_reached_is_409(self, mock_resolve, *_):
        from terrapod.services.summariser import FollowupCapReached

        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        summary = _mock_cost_summary(run.id)
        mock_resolve.return_value = (run, summary, ws)

        app = _make_app(_user(), AsyncMock())
        with patch(
            "terrapod.services.cost_summariser.post_cost_followup",
            new_callable=AsyncMock,
            side_effect=FollowupCapReached("cap hit"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary/messages",
                    headers=_AUTH,
                    json={"data": {"attributes": {"content": "hi"}}},
                )
        assert resp.status_code == 409

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs._resolve_cost_summary_for_chat")
    async def test_post_disabled_is_503(self, mock_resolve, *_):
        from terrapod.services.summariser import FollowupDisabled

        run = _mock_run()
        ws = _mock_ws(run.workspace_id)
        summary = _mock_cost_summary(run.id)
        mock_resolve.return_value = (run, summary, ws)

        app = _make_app(_user(), AsyncMock())
        with patch(
            "terrapod.services.cost_summariser.post_cost_followup",
            new_callable=AsyncMock,
            side_effect=FollowupDisabled("off"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/cost-summary/messages",
                    headers=_AUTH,
                    json={"data": {"attributes": {"content": "hi"}}},
                )
        assert resp.status_code == 503
