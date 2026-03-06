"""Tests for the audit service."""

from unittest.mock import AsyncMock, MagicMock

from terrapod.services.audit_service import (
    log_audit_event,
    parse_resource,
    purge_old_entries,
    query_audit_log,
    should_audit,
)


class TestShouldAudit:
    def test_health_excluded(self):
        assert should_audit("/health") is False

    def test_ready_excluded(self):
        assert should_audit("/ready") is False

    def test_docs_excluded(self):
        assert should_audit("/api/docs") is False

    def test_redoc_excluded(self):
        assert should_audit("/api/redoc") is False

    def test_openapi_excluded(self):
        assert should_audit("/api/openapi.json") is False

    def test_api_endpoint_included(self):
        assert should_audit("/api/v2/workspaces") is True

    def test_oauth_endpoint_included(self):
        assert should_audit("/oauth/authorize") is True

    def test_root_included(self):
        assert should_audit("/") is True


class TestParseResource:
    def test_workspace_with_id(self):
        rtype, rid = parse_resource("/api/v2/workspaces/ws-abc123")
        assert rtype == "workspaces"
        assert rid == "ws-abc123"

    def test_workspace_list(self):
        rtype, rid = parse_resource("/api/v2/organizations/default/workspaces")
        assert rtype == "workspaces"
        assert rid == ""

    def test_runs_nested(self):
        rtype, rid = parse_resource("/api/v2/runs/run-xyz")
        assert rtype == "runs"
        assert rid == "run-xyz"

    def test_admin_audit_log(self):
        rtype, rid = parse_resource("/api/v2/admin/audit-log")
        assert rtype == "admin"
        assert rid == "audit-log"

    def test_oauth_path(self):
        rtype, rid = parse_resource("/oauth/authorize")
        assert rtype == "oauth"
        assert rid == ""

    def test_empty_path(self):
        rtype, rid = parse_resource("/")
        assert rtype == ""
        assert rid == ""

    def test_state_versions(self):
        rtype, rid = parse_resource("/api/v2/state-versions/sv-123/content")
        assert rtype == "state-versions"
        assert rid == "sv-123"


class TestLogAuditEvent:
    async def test_creates_entry(self):
        """log_audit_event inserts an AuditLog and commits."""
        mock_db = AsyncMock()

        await log_audit_event(
            mock_db,
            actor_email="user@example.com",
            actor_ip="10.0.0.1",
            action="POST",
            resource_type="workspaces",
            resource_id="ws-abc",
            status_code=201,
            request_id="req-xyz",
            duration_ms=15,
        )

        mock_db.add.assert_called_once()
        entry = mock_db.add.call_args[0][0]
        assert entry.actor_email == "user@example.com"
        assert entry.action == "POST"
        assert entry.resource_type == "workspaces"
        assert entry.status_code == 201
        assert entry.duration_ms == 15
        mock_db.commit.assert_awaited_once()

    async def test_defaults_empty_strings(self):
        """Calling with minimal args uses empty defaults."""
        mock_db = AsyncMock()

        await log_audit_event(
            mock_db,
            action="GET",
            status_code=200,
        )

        entry = mock_db.add.call_args[0][0]
        assert entry.actor_email == ""
        assert entry.actor_ip == ""
        assert entry.resource_type == ""
        assert entry.resource_id == ""


class TestPurgeOldEntries:
    async def test_deletes_old_entries(self):
        """purge_old_entries executes delete and commits."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 5
        mock_db.execute.return_value = mock_result

        deleted = await purge_old_entries(mock_db, retention_days=30)

        assert deleted == 5
        mock_db.execute.assert_awaited_once()
        mock_db.commit.assert_awaited_once()

    async def test_zero_deleted(self):
        """No old entries → returns 0."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_db.execute.return_value = mock_result

        deleted = await purge_old_entries(mock_db, retention_days=90)
        assert deleted == 0


class TestQueryAuditLog:
    async def test_returns_entries_and_count(self):
        """query_audit_log returns list and total count."""
        entry = MagicMock()
        entry.id = "abc"

        mock_db = AsyncMock()
        # First execute: count query
        count_result = MagicMock()
        count_result.all.return_value = [("id1",), ("id2",), ("id3",)]
        # Second execute: data query
        data_result = MagicMock()
        data_result.scalars.return_value.all.return_value = [entry]

        mock_db.execute.side_effect = [count_result, data_result]

        entries, total = await query_audit_log(mock_db, page_number=1, page_size=10)

        assert total == 3
        assert entries == [entry]
        assert mock_db.execute.await_count == 2

    async def test_pagination_offset(self):
        """Page 3 with size 5 applies correct offset."""
        mock_db = AsyncMock()
        count_result = MagicMock()
        count_result.all.return_value = []
        data_result = MagicMock()
        data_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [count_result, data_result]

        entries, total = await query_audit_log(mock_db, page_number=3, page_size=5)

        assert total == 0
        assert entries == []
