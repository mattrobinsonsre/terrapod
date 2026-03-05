"""Tests for the audit service."""

from terrapod.services.audit_service import parse_resource, should_audit


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
