"""Tests for notification payload building, signing, and delivery providers."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

from terrapod.services.notification_service import (
    VALID_TRIGGERS,
    build_run_payload,
    build_verification_payload,
    deliver_generic,
    deliver_slack,
    record_delivery_response,
    sign_payload,
)


class TestBuildRunPayload:
    def test_payload_structure(self):
        payload = build_run_payload(
            nc_name="test-notif",
            run_id="run-abc",
            run_status="applied",
            run_created_at="2026-01-01T00:00:00Z",
            workspace_id="ws-123",
            workspace_name="my-ws",
            trigger="run:completed",
            run_message="Test run",
        )

        assert payload["payload_version"] == 1
        assert payload["notification_configuration_id"] == "test-notif"
        assert payload["run_id"] == "run-abc"
        assert payload["workspace_name"] == "my-ws"
        assert payload["organization_name"] == "default"
        assert len(payload["notifications"]) == 1
        assert payload["notifications"][0]["trigger"] == "run:completed"
        assert payload["notifications"][0]["run_status"] == "applied"

    def test_empty_message(self):
        payload = build_run_payload(
            nc_name="x",
            run_id="r",
            run_status="pending",
            run_created_at="",
            workspace_id="w",
            workspace_name="ws",
            trigger="run:created",
        )
        assert payload["run_message"] == ""


class TestBuildVerificationPayload:
    def test_null_fields(self):
        payload = build_verification_payload("my-config")
        assert payload["run_id"] is None
        assert payload["workspace_id"] is None
        assert payload["workspace_name"] is None
        assert payload["notifications"][0]["trigger"] == "verification"
        assert payload["notifications"][0]["run_status"] is None


class TestSignPayload:
    def test_hmac_sha512(self):
        body = b'{"test": true}'
        token = "my-secret"
        sig = sign_payload(body, token)

        expected = hmac.new(token.encode(), body, hashlib.sha512).hexdigest()
        assert sig == expected

    def test_different_tokens_different_sigs(self):
        body = b"hello"
        sig1 = sign_payload(body, "token-a")
        sig2 = sign_payload(body, "token-b")
        assert sig1 != sig2


class TestDeliverGeneric:
    @patch("terrapod.services.notification_service.httpx.AsyncClient")
    async def test_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await deliver_generic("https://example.com/hook", {"msg": "test"})
        assert result["success"] is True
        assert result["status"] == 200

    @patch("terrapod.services.notification_service.httpx.AsyncClient")
    async def test_with_token_sends_signature(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await deliver_generic("https://example.com", {"test": True}, token="secret")
        call_args = mock_client.post.call_args
        headers = call_args.kwargs.get("headers", {})
        assert "X-TFE-Notification-Signature" in headers

    @patch("terrapod.services.notification_service.httpx.AsyncClient")
    async def test_server_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await deliver_generic("https://example.com", {})
        assert result["success"] is False
        assert result["status"] == 500

    @patch("terrapod.services.notification_service.httpx.AsyncClient")
    async def test_connection_error(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await deliver_generic("https://bad.example.com", {})
        assert result["success"] is False
        assert result["status"] == 0
        assert "Connection refused" in result["body"]


class TestDeliverSlack:
    @patch("terrapod.services.notification_service.httpx.AsyncClient")
    async def test_slack_block_kit_format(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        payload = build_run_payload(
            nc_name="test",
            run_id="r-1",
            run_status="applied",
            run_created_at="",
            workspace_id="ws-1",
            workspace_name="test-ws",
            trigger="run:completed",
        )

        result = await deliver_slack("https://hooks.slack.com/test", payload)
        assert result["success"] is True

        # Verify the posted content has blocks
        call_args = mock_client.post.call_args
        posted = json.loads(call_args.kwargs.get("content", b"{}"))
        assert "blocks" in posted
        assert posted["blocks"][0]["type"] == "header"


class TestRecordDeliveryResponse:
    async def test_caps_at_max(self):
        nc = MagicMock()
        nc.delivery_responses = [{"status": 200, "body": "ok"} for _ in range(10)]
        nc.id = "test-id"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = nc

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        await record_delivery_response(
            mock_db, nc.id, {"status": 201, "body": "new"}, max_responses=5
        )

        # Should have capped to 5
        assert len(nc.delivery_responses) == 5
        # Last item should be the new one
        assert nc.delivery_responses[-1]["status"] == 201

    async def test_nc_not_found(self):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result

        # Should not raise
        await record_delivery_response(mock_db, "nonexistent", {"status": 200})


class TestValidTriggers:
    def test_all_expected_triggers_present(self):
        expected = {
            "run:created",
            "run:planning",
            "run:needs_attention",
            "run:planned",
            "run:applying",
            "run:completed",
            "run:errored",
            "run:drift_detected",
        }
        assert VALID_TRIGGERS == expected
