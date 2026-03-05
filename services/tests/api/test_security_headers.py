"""Tests for security headers middleware."""

import pytest
from fastapi.testclient import TestClient

from terrapod.api.app import create_application


@pytest.fixture
def client():
    """Create a test client."""
    app = create_application()
    return TestClient(app, raise_server_exceptions=False)


class TestSecurityHeaders:
    def test_health_endpoint_has_security_headers(self, client):
        """Security headers are present on health check responses."""
        response = client.get("/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Permissions-Policy"] == "geolocation=(), microphone=(), camera=()"

    def test_api_endpoint_has_security_headers(self, client):
        """Security headers are present on API responses (even 401)."""
        response = client.get("/api/v2/ping")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"

    def test_request_id_header_present(self, client):
        """X-Request-ID header is present on responses."""
        response = client.get("/health")
        assert "X-Request-ID" in response.headers
